"""Desktop-native presentation generation orchestration."""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import contextvars
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.presentation.pptx_writer import (
    PRESENTATION_MIME_TYPE,
    PresentationSlide,
    write_image_presentation,
)


MAX_PRESENTATION_PAGES = 40
MAX_IMAGE_DOWNLOAD_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_MAX_RETRIES = 3
PRESENTATION_REVISION_LOCK_MAX_AGE_SECONDS = 6 * 60 * 60

IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)

CONTENT_TYPE_EXTENSIONS = {
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "image/webp": "webp",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted
    except Exception:
        return False
    return bool(is_interrupted())


def _safe_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", normalized)
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-. ")
    return (normalized or "presentation")[:80]


def _ensure_inside_workspace(path_value: str, workspace_root: str) -> str:
    resolved = os.path.realpath(os.path.abspath(path_value))
    root = os.path.realpath(os.path.abspath(workspace_root))
    try:
        inside = os.path.commonpath([root, resolved]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("presentation output must stay inside the active workspace")
    return resolved


def _http_url(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized if normalized.lower().startswith(("http://", "https://")) else None


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _revision_lock_is_stale(lock_path: str) -> bool:
    try:
        age = max(0.0, time.time() - os.path.getmtime(lock_path))
        if age < PRESENTATION_REVISION_LOCK_MAX_AGE_SECONDS:
            return False
        with open(lock_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return not _process_is_alive(int(payload.get("pid") or 0))
    except FileNotFoundError:
        return False
    except Exception:
        return False


@contextmanager
def _presentation_revision_lock(presentation_path: str):
    destination = Path(presentation_path)
    lock_path = str(destination.with_name(f".{destination.name}.dovie-revision.lock"))
    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if attempt == 0 and _revision_lock_is_stale(lock_path):
                try:
                    os.unlink(lock_path)
                except FileNotFoundError:
                    pass
                continue
            raise RuntimeError("this presentation is already being regenerated")
    else:
        raise RuntimeError("could not acquire the presentation revision lock")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": _now_iso()}, handle)
        yield
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def _unique_output_path(path_value: str, overwrite: bool) -> str:
    if overwrite or not os.path.exists(path_value):
        return path_value
    parsed = Path(path_value)
    for index in range(2, 10_000):
        candidate = str(parsed.with_name(f"{parsed.stem}-{index}{parsed.suffix}"))
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError("could not allocate a unique presentation output path")


def _atomic_json(path_value: str, value: dict[str, Any]) -> None:
    destination = Path(path_value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_limited(
    response,
    *,
    interrupted: Callable[[], bool],
    max_bytes: int = MAX_IMAGE_DOWNLOAD_BYTES,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if interrupted():
            raise InterruptedError("presentation generation interrupted")
        chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"generated slide image exceeds {max_bytes} bytes")
    return b"".join(chunks)


def _data_url_bytes(source: str) -> tuple[bytes, str]:
    header, separator, payload = source.partition(",")
    if not separator or not header.lower().startswith("data:image/"):
        raise ValueError("invalid generated image data URL")
    media_type = header[5:].split(";", 1)[0].lower()
    if ";base64" not in header.lower():
        return urllib.parse.unquote_to_bytes(payload), media_type
    try:
        return base64.b64decode(payload, validate=True), media_type
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid generated image base64 payload") from exc


def _download_image(
    source: str,
    *,
    interrupted: Callable[[], bool],
) -> tuple[bytes, str]:
    if source.lower().startswith("data:image/"):
        data, media_type = _data_url_bytes(source)
        if len(data) > MAX_IMAGE_DOWNLOAD_BYTES:
            raise ValueError(f"generated slide image exceeds {MAX_IMAGE_DOWNLOAD_BYTES} bytes")
        return data, media_type
    if not source.lower().startswith(("http://", "https://")):
        raise ValueError("generated slide image must be an HTTP(S) or data URL")
    request = urllib.request.Request(
        source,
        headers={
            "Accept": "image/*",
            "User-Agent": "Dovie-Desktop-Presentation/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                raise RuntimeError(f"slide image download returned HTTP {status}")
            media_type = str(response.headers.get_content_type() or "").lower()
            return _read_limited(response, interrupted=interrupted), media_type
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"slide image download returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"slide image download failed: {exc.reason}") from exc


def _image_extension(data: bytes, media_type: str) -> str:
    for magic, extension in IMAGE_MAGIC:
        if data.startswith(magic):
            return extension
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    extension = CONTENT_TYPE_EXTENSIONS.get(str(media_type or "").split(";", 1)[0].lower())
    if extension:
        return extension
    raise ValueError("generated slide image format is unsupported")


def _save_slide_image(
    image_source: str,
    destination_root: str,
    page_number: int,
    *,
    interrupted: Callable[[], bool],
) -> tuple[str, str]:
    data, media_type = _download_image(image_source, interrupted=interrupted)
    extension = _image_extension(data, media_type)
    destination = Path(destination_root) / f"page-{page_number:03d}.{extension}"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return str(destination), extension


def _interruptible_backoff(
    seconds: float,
    *,
    interrupted: Callable[[], bool],
) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if interrupted():
            raise InterruptedError("presentation generation interrupted")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.1))


def _parse_generator_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        result = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("image generator returned a non-JSON result") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("image generator returned an invalid result")
        result = parsed
    else:
        raise RuntimeError("image generator returned an invalid result")
    error = str(result.get("error") or result.get("error_message") or "").strip()
    if error:
        raise RuntimeError(error)
    status = str(result.get("status") or "").strip().lower()
    if status in {"cancelled", "canceled"}:
        raise InterruptedError("presentation generation interrupted")
    if status in {"failed", "error"}:
        raise RuntimeError(str(result.get("message") or "image generation failed"))
    return result


def _first_generated_image(result: dict[str, Any]) -> str:
    urls = result.get("image_urls")
    if isinstance(urls, list):
        for value in urls:
            if isinstance(value, str) and value.strip():
                return value.strip()
    encoded = result.get("base64_data")
    if isinstance(encoded, list):
        for value in encoded:
            if not isinstance(value, str) or not value.strip():
                continue
            normalized = value.strip()
            return normalized if normalized.startswith("data:image/") else f"data:image/png;base64,{normalized}"
    raise RuntimeError("image generation completed without an image")


def _page_title(page_titles: list[str], prompts: list[str], index: int) -> str:
    if index < len(page_titles) and page_titles[index]:
        return page_titles[index]
    prompt = prompts[index].strip().splitlines()[0]
    return prompt[:80] or f"Slide {index + 1}"


def _page_payload(
    *,
    index: int,
    prompts: list[str],
    page_titles: list[str],
    references: list[str | None],
) -> dict[str, Any]:
    return {
        "index": index,
        "page_number": index + 1,
        "title": _page_title(page_titles, prompts, index),
        "prompt": prompts[index],
        "reference_image_url": references[index] if index < len(references) else None,
        "status": "pending",
        "attempts": 0,
        "image_url": None,
        "image_path": None,
        "image_extension": None,
        "error": None,
    }


def _result_summary(manifest: dict[str, Any], *, include_errors: bool = True) -> dict[str, Any]:
    pages = manifest["pages"]
    failed_pages = [
        {
            "page_number": page["page_number"],
            "title": page["title"],
            "error": page.get("error"),
        }
        for page in pages
        if page["status"] == "failed"
    ]
    result = {
        "title": manifest["title"],
        "aspect_ratio": manifest["aspect_ratio"],
        "total_pages": len(pages),
        "success_count": sum(page["status"] == "completed" for page in pages),
        "failed_count": len(failed_pages),
        "cancelled_count": sum(page["status"] == "cancelled" for page in pages),
        "output_path": manifest.get("output_path"),
        "manifest_path": manifest["manifest_path"],
        "failed_pages": failed_pages if include_errors else [],
    }
    return result


def _validate_request(args: dict[str, Any], workspace_root: str) -> dict[str, Any]:
    title = str(args.get("title") or "Untitled presentation").strip()
    prompts_value = args.get("page_prompts")
    if not isinstance(prompts_value, list):
        raise ValueError("page_prompts must be an array")
    prompts = [str(value or "").strip() for value in prompts_value]
    if not prompts or any(not value for value in prompts):
        raise ValueError("page_prompts must contain at least one non-empty prompt")
    if len(prompts) > MAX_PRESENTATION_PAGES:
        raise ValueError(f"presentations support at most {MAX_PRESENTATION_PAGES} pages")

    aspect_ratio = str(args.get("aspect_ratio") or "16:9").strip()
    if aspect_ratio not in {"16:9", "4:3", "1:1"}:
        raise ValueError("aspect_ratio must be one of 16:9, 4:3, or 1:1")

    titles_value = args.get("page_titles")
    page_titles = (
        [str(value or "").strip() for value in titles_value]
        if isinstance(titles_value, list)
        else []
    )
    if page_titles and len(page_titles) != len(prompts):
        raise ValueError("page_titles must have the same length as page_prompts")

    references_value = args.get("page_reference_images")
    references = (
        [str(value or "").strip() or None for value in references_value]
        if isinstance(references_value, list)
        else [None] * len(prompts)
    )
    if references and len(references) != len(prompts):
        raise ValueError("page_reference_images must have the same length as page_prompts")

    raw_output = str(args.get("output_path") or "").strip()
    output = raw_output or os.path.join("presentations", f"{_safe_slug(title)}.pptx")
    if not os.path.isabs(output):
        output = os.path.join(workspace_root, output)
    if not output.lower().endswith(".pptx"):
        output += ".pptx"
    output = _ensure_inside_workspace(output, workspace_root)
    output = _unique_output_path(output, bool(args.get("overwrite", False)))

    return {
        "title": title,
        "prompts": prompts,
        "page_titles": page_titles,
        "references": references,
        "aspect_ratio": aspect_ratio,
        "style_prefix": str(args.get("style_prefix") or "").strip(),
        "output_path": output,
        "model": str(args.get("model") or "").strip(),
        "quality": str(args.get("quality") or "2K").strip(),
        "max_concurrent": max(
            1,
            min(int(args.get("max_concurrent") or DEFAULT_MAX_CONCURRENT), 6),
        ),
        "max_retries": max(
            1,
            min(int(args.get("max_retries") or DEFAULT_MAX_RETRIES), 5),
        ),
    }


def _load_embedded_presentation(
    presentation_path: str,
    extraction_root: str,
) -> tuple[dict[str, Any], list[PresentationSlide]]:
    try:
        with zipfile.ZipFile(presentation_path, "r") as archive:
            try:
                manifest_info = archive.getinfo("dovie/presentation.json")
                if manifest_info.file_size > 1024 * 1024:
                    raise ValueError("the embedded Dovie presentation manifest is too large")
                manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            except KeyError as exc:
                raise ValueError(
                    "single-slide regeneration is only supported for Dovie image presentations"
                ) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("the embedded Dovie presentation manifest is invalid") from exc
            if not isinstance(manifest, dict) or manifest.get("kind") != "dovie.image_presentation":
                raise ValueError(
                    "single-slide regeneration is only supported for Dovie image presentations"
                )
            raw_slides = manifest.get("slides")
            if not isinstance(raw_slides, list) or not raw_slides:
                raise ValueError("the embedded Dovie presentation has no slides")
            if len(raw_slides) > MAX_PRESENTATION_PAGES:
                raise ValueError(
                    f"Dovie presentations support at most {MAX_PRESENTATION_PAGES} slides"
                )

            slides: list[PresentationSlide] = []
            for index, raw_slide in enumerate(raw_slides):
                if not isinstance(raw_slide, dict):
                    raise ValueError("the embedded Dovie presentation slide metadata is invalid")
                media_path = str(raw_slide.get("media_path") or "").strip()
                normalized_media_path = media_path.replace("\\", "/")
                if (
                    not normalized_media_path.startswith("ppt/media/")
                    or ".." in normalized_media_path.split("/")
                ):
                    raise ValueError("the embedded Dovie presentation media path is invalid")
                extension = Path(normalized_media_path).suffix.lower().lstrip(".")
                if extension not in CONTENT_TYPE_EXTENSIONS.values():
                    raise ValueError(f"unsupported presentation image extension: {extension}")
                try:
                    image_info = archive.getinfo(normalized_media_path)
                except KeyError as exc:
                    raise ValueError(
                        f"presentation slide {index + 1} image is missing"
                    ) from exc
                if image_info.file_size > MAX_IMAGE_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"presentation slide {index + 1} image exceeds "
                        f"{MAX_IMAGE_DOWNLOAD_BYTES} bytes"
                    )
                image_bytes = archive.read(image_info)
                image_path = os.path.join(
                    extraction_root,
                    f"page-{index + 1:03d}.{extension}",
                )
                with open(image_path, "wb") as handle:
                    handle.write(image_bytes)
                slides.append(
                    PresentationSlide(
                        page_number=int(raw_slide.get("page_number") or index + 1),
                        title=str(raw_slide.get("title") or f"Slide {index + 1}"),
                        prompt=str(raw_slide.get("prompt") or ""),
                        image_path=image_path,
                        image_extension=extension,
                        image_url=_http_url(raw_slide.get("image_url")),
                    )
                )
    except zipfile.BadZipFile as exc:
        raise ValueError("the presentation file is not a valid PPTX") from exc
    return manifest, slides


def _validate_revision_request(args: dict[str, Any], workspace_root: str) -> dict[str, Any]:
    raw_path = str(args.get("presentation_path") or args.get("path") or "").strip()
    if not raw_path:
        raise ValueError("presentation_path is required")
    presentation_path = raw_path
    if not os.path.isabs(presentation_path):
        presentation_path = os.path.join(workspace_root, presentation_path)
    presentation_path = _ensure_inside_workspace(presentation_path, workspace_root)
    if not presentation_path.lower().endswith(".pptx"):
        raise ValueError("presentation_path must point to a .pptx file")
    if not os.path.isfile(presentation_path):
        raise ValueError("presentation file not found")

    try:
        page_number = int(args.get("page_number"))
    except (TypeError, ValueError) as exc:
        raise ValueError("page_number must be a positive integer") from exc
    if page_number <= 0:
        raise ValueError("page_number must be a positive integer")
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if len(prompt) > 4000:
        raise ValueError("prompt must not exceed 4000 characters")
    reference_image_url = str(args.get("reference_image_url") or "").strip()
    return {
        "presentation_path": presentation_path,
        "page_number": page_number,
        "prompt": prompt,
        "title": str(args.get("title") or "").strip(),
        "model": str(args.get("model") or "").strip(),
        "quality": str(args.get("quality") or "").strip(),
        "reference_image_url": reference_image_url or None,
        "use_current_slide_as_reference": bool(
            args.get("use_current_slide_as_reference", True)
        ),
    }


def generate_presentation(
    args: dict[str, Any],
    *,
    image_generator: Callable[[dict[str, Any]], Any],
    workspace_root: str | None = None,
    interrupted: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Generate slide images, checkpoint locally, and assemble an atomic PPTX."""

    root = os.path.abspath(workspace_root or os.getcwd())
    is_interrupted = interrupted or _default_interrupted
    request = _validate_request(args, root)
    job_id = f"presentation-{uuid.uuid4().hex}"
    job_root = os.path.join(root, ".dovie", "presentations", job_id)
    os.makedirs(job_root, exist_ok=True)
    manifest_path = os.path.join(job_root, "manifest.json")
    pages = [
        _page_payload(
            index=index,
            prompts=request["prompts"],
            page_titles=request["page_titles"],
            references=request["references"],
        )
        for index in range(len(request["prompts"]))
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "job_id": job_id,
        "status": "running",
        "title": request["title"],
        "aspect_ratio": request["aspect_ratio"],
        "output_path": request["output_path"],
        "manifest_path": manifest_path,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "pages": pages,
    }
    manifest_lock = threading.Lock()

    def checkpoint() -> None:
        with manifest_lock:
            manifest["updated_at"] = _now_iso()
            _atomic_json(manifest_path, deepcopy(manifest))

    def run_page(index: int) -> dict[str, Any]:
        page = deepcopy(pages[index])
        full_prompt = (
            f'{request["style_prefix"]} {page["prompt"]}'.strip()
            if request["style_prefix"]
            else page["prompt"]
        )
        last_error = ""
        for attempt in range(1, request["max_retries"] + 1):
            if is_interrupted():
                raise InterruptedError("presentation generation interrupted")
            page["attempts"] = attempt
            try:
                generator_args: dict[str, Any] = {
                    "prompt": full_prompt,
                    "aspect_ratio": request["aspect_ratio"],
                    "generate_num": 1,
                    "quality": request["quality"],
                }
                if request["model"]:
                    generator_args["model"] = request["model"]
                if page["reference_image_url"]:
                    generator_args["reference_image_url"] = page["reference_image_url"]
                result = _parse_generator_result(image_generator(generator_args))
                source = _first_generated_image(result)
                image_path, extension = _save_slide_image(
                    source,
                    job_root,
                    page["page_number"],
                    interrupted=is_interrupted,
                )
                page.update(
                    {
                        "status": "completed",
                        "image_url": source if source.startswith(("http://", "https://")) else None,
                        "image_path": image_path,
                        "image_extension": extension,
                        "error": None,
                    }
                )
                return page
            except InterruptedError:
                raise
            except Exception as exc:
                last_error = str(exc)
                if attempt < request["max_retries"]:
                    _interruptible_backoff(
                        min(2 ** (attempt - 1), 4),
                        interrupted=is_interrupted,
                    )
        page.update({"status": "failed", "error": last_error or "slide generation failed"})
        return page

    checkpoint()
    futures: dict[concurrent.futures.Future, int] = {}
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=request["max_concurrent"],
        thread_name_prefix="dovie-presentation",
    )
    cancelled = False
    try:
        for index in range(len(pages)):
            page_context = contextvars.copy_context()
            futures[executor.submit(page_context.run, run_page, index)] = index
        pending = set(futures)
        while pending:
            if is_interrupted():
                cancelled = True
                for future in pending:
                    future.cancel()
            done, pending = concurrent.futures.wait(
                pending,
                timeout=0.2,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                index = futures[future]
                if future.cancelled():
                    pages[index].update(
                        {"status": "cancelled", "error": "presentation generation interrupted"}
                    )
                else:
                    try:
                        pages[index] = future.result()
                    except InterruptedError:
                        cancelled = True
                        pages[index].update(
                            {"status": "cancelled", "error": "presentation generation interrupted"}
                        )
                    except Exception as exc:
                        pages[index].update({"status": "failed", "error": str(exc)})
                checkpoint()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if cancelled:
        for page in pages:
            if page["status"] == "pending":
                page.update({"status": "cancelled", "error": "presentation generation interrupted"})
        manifest["status"] = "cancelled"
        checkpoint()
        return {
            "dovie_event": "presentation_generation_cancelled",
            "status": "cancelled",
            "message": "Presentation generation was interrupted. The checkpoint was preserved.",
            **_result_summary(manifest),
            "artifacts": [],
        }

    failed = [page for page in pages if page["status"] != "completed"]
    if failed:
        manifest["status"] = "failed"
        checkpoint()
        summary = _result_summary(manifest)
        return {
            "dovie_event": "presentation_generation_failed",
            "status": "failed",
            "message": "Presentation generation failed. The checkpoint and completed slide images were preserved.",
            "error": "; ".join(
                f'page {page["page_number"]}: {page.get("error") or "failed"}'
                for page in failed
            ),
            **summary,
            "artifacts": [],
        }

    slides = [
        PresentationSlide(
            page_number=page["page_number"],
            title=page["title"],
            prompt=page["prompt"],
            image_path=page["image_path"],
            image_extension=page["image_extension"],
            image_url=_http_url(page.get("image_url")),
        )
        for page in pages
    ]
    try:
        write_image_presentation(
            request["output_path"],
            title=request["title"],
            aspect_ratio=request["aspect_ratio"],
            slides=slides,
            metadata={
                "job_id": job_id,
                "generator": "dovie_image_generate",
                "model": request["model"] or None,
                "quality": request["quality"],
                "style_prefix": request["style_prefix"] or None,
                "revision": 0,
            },
            interrupted=is_interrupted,
        )
    except InterruptedError:
        manifest["status"] = "cancelled"
        manifest["assembly_error"] = "presentation generation interrupted"
        checkpoint()
        return {
            "dovie_event": "presentation_generation_cancelled",
            "status": "cancelled",
            "message": "Presentation assembly was interrupted. The checkpoint and slide images were preserved.",
            **_result_summary(manifest),
            "artifacts": [],
        }
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["assembly_error"] = str(exc)
        checkpoint()
        return {
            "dovie_event": "presentation_generation_failed",
            "status": "failed",
            "message": "Presentation assembly failed. The checkpoint and slide images were preserved.",
            "error": f"presentation assembly failed: {exc}",
            **_result_summary(manifest),
            "artifacts": [],
        }
    manifest["status"] = "completed"
    manifest["completed_at"] = _now_iso()
    checkpoint()
    return {
        "dovie_event": "presentation_generation_completed",
        "status": "completed",
        "message": f'Created presentation "{request["title"]}".',
        **_result_summary(manifest),
        "artifacts": [
            {
                "path": request["output_path"],
                "title": request["title"],
                "mime_type": PRESENTATION_MIME_TYPE,
                "operation": "created",
            }
        ],
    }


def regenerate_presentation_slide(
    args: dict[str, Any],
    *,
    image_generator: Callable[[dict[str, Any]], Any],
    workspace_root: str | None = None,
    interrupted: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Regenerate one image-backed slide and atomically replace its PPTX."""

    root = os.path.abspath(workspace_root or os.getcwd())
    is_interrupted = interrupted or _default_interrupted
    request = _validate_revision_request(args, root)
    presentation_path = request["presentation_path"]

    with _presentation_revision_lock(presentation_path):
        if is_interrupted():
            raise InterruptedError("presentation slide regeneration interrupted")
        with tempfile.TemporaryDirectory(
            prefix=".dovie-presentation-revision-",
            dir=str(Path(presentation_path).parent),
        ) as extraction_root:
            manifest, slides = _load_embedded_presentation(
                presentation_path,
                extraction_root,
            )
            page_number = request["page_number"]
            if page_number > len(slides):
                raise ValueError(
                    f"page_number {page_number} exceeds the presentation's {len(slides)} slides"
                )
            slide_index = page_number - 1
            current_slide = slides[slide_index]
            metadata = (
                dict(manifest.get("metadata"))
                if isinstance(manifest.get("metadata"), dict)
                else {}
            )
            style_prefix = str(metadata.get("style_prefix") or "").strip()
            full_prompt = (
                f'{style_prefix} {request["prompt"]}'.strip()
                if style_prefix
                else request["prompt"]
            )
            model = request["model"] or str(metadata.get("model") or "").strip()
            quality = request["quality"] or str(metadata.get("quality") or "2K").strip()
            reference_image_url = request["reference_image_url"]
            if not reference_image_url and request["use_current_slide_as_reference"]:
                reference_image_url = _http_url(current_slide.image_url)

            generator_args: dict[str, Any] = {
                "prompt": full_prompt,
                "aspect_ratio": str(manifest.get("aspect_ratio") or "16:9"),
                "generate_num": 1,
                "quality": quality,
            }
            if model:
                generator_args["model"] = model
            if reference_image_url:
                generator_args["reference_image_url"] = reference_image_url

            try:
                result = _parse_generator_result(image_generator(generator_args))
            except Exception:
                inherited_reference = (
                    reference_image_url
                    and not request["reference_image_url"]
                    and request["use_current_slide_as_reference"]
                )
                if not inherited_reference:
                    raise
                generator_args.pop("reference_image_url", None)
                result = _parse_generator_result(image_generator(generator_args))
            source = _first_generated_image(result)
            revised_image_path, extension = _save_slide_image(
                source,
                extraction_root,
                page_number,
                interrupted=is_interrupted,
            )
            revised_title = request["title"] or current_slide.title
            slides[slide_index] = PresentationSlide(
                page_number=current_slide.page_number,
                title=revised_title,
                prompt=request["prompt"],
                image_path=revised_image_path,
                image_extension=extension,
                image_url=_http_url(source),
            )

            try:
                revision = int(metadata.get("revision") or 0) + 1
            except (TypeError, ValueError):
                revision = 1
            revised_at = _now_iso()
            history = metadata.get("revision_history")
            normalized_history = list(history) if isinstance(history, list) else []
            normalized_history.append(
                {
                    "revision": revision,
                    "page_number": page_number,
                    "prompt": request["prompt"],
                    "revised_at": revised_at,
                }
            )
            metadata.update(
                {
                    "generator": "dovie_image_generate",
                    "model": model or None,
                    "quality": quality,
                    "revision": revision,
                    "last_modified_at": revised_at,
                    "last_regenerated_page": page_number,
                    "revision_history": normalized_history[-50:],
                }
            )
            write_image_presentation(
                presentation_path,
                title=str(manifest.get("title") or Path(presentation_path).stem),
                aspect_ratio=str(manifest.get("aspect_ratio") or "16:9"),
                slides=slides,
                metadata=metadata,
                interrupted=is_interrupted,
            )

    return {
        "dovie_event": "presentation_slide_regenerated",
        "status": "completed",
        "message": f"Regenerated slide {request['page_number']}.",
        "title": str(manifest.get("title") or Path(presentation_path).stem),
        "output_path": presentation_path,
        "page_number": request["page_number"],
        "slide_title": slides[request["page_number"] - 1].title,
        "prompt": request["prompt"],
        "revision": metadata["revision"],
        "artifacts": [
            {
                "path": presentation_path,
                "title": str(manifest.get("title") or Path(presentation_path).stem),
                "mime_type": PRESENTATION_MIME_TYPE,
                "operation": "modified",
            }
        ],
    }
