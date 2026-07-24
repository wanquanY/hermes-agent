"""Dovie-managed image and video generation tools.

These tools mirror ``dovie_web``: packaged Dovie Hermes runtimes call
authenticated backend proxy endpoints with the llm-runtime token, while
provider credentials and routing stay on the Dovie backend.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from tools.registry import registry, tool_error

DOVIE_MEDIA_PROXY_DEFAULT_TIMEOUT_SECONDS = 310
DOVIE_MEDIA_PROXY_MAX_TIMEOUT_SECONDS = 1800
DOVIE_MEDIA_PROXY_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DOVIE_MEDIA_PROXY_DEFAULT_POLL_INTERVAL_SECONDS = 2
DOVIE_MEDIA_REFERENCE_DEFAULT_MAX_BYTES = 20 * 1024 * 1024


DOVIE_IMAGE_GENERATE_SCHEMA = {
    "name": "dovie_image_generate",
    "description": (
        "Generate images through Dovie's managed image generation backend. "
        "Use this instead of image_generate in Dovie runtimes because it is "
        "ready out of the box and keeps provider credentials server-side."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Image prompt.",
                "minLength": 1,
                "maxLength": 2000,
            },
            "negative_prompt": {
                "type": "string",
                "description": "Optional negative prompt.",
                "maxLength": 1000,
            },
            "aspect_ratio": {
                "type": "string",
                "description": (
                    "Optional output aspect ratio. Omit unless the user requests one; "
                    "the Dovie Admin model configuration supplies the default."
                ),
                "enum": [
                    "21:9", "16:9", "3:2", "4:3", "5:4", "1:1",
                    "4:5", "3:4", "2:3", "9:16",
                    "landscape", "portrait", "square",
                ],
            },
            "generate_num": {
                "type": "integer",
                "description": (
                    "Optional number of images. Omit unless the user requests a count; "
                    "the Dovie Admin model configuration supplies the default."
                ),
                "minimum": 1,
                "maximum": 4,
            },
            "model": {
                "type": "string",
                "description": "Optional Dovie image model id. Omit for the backend default.",
            },
            "reference_image_url": {
                "type": "string",
                "description": (
                    "Optional HTTP(S) URL or local workspace/attached-image path "
                    "for image-to-image."
                ),
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional HTTP(S) URLs or local workspace/attached-image paths "
                    "for multi-image-to-image."
                ),
            },
            "quality": {
                "type": "string",
                "description": (
                    "Optional provider-native quality override. Omit unless the user "
                    "explicitly requests a quality; never infer a value. The Dovie Admin "
                    "model configuration supplies the supported default."
                ),
            },
        },
        "required": ["prompt"],
    },
}


DOVIE_VIDEO_GENERATE_SCHEMA = {
    "name": "dovie_video_generate",
    "description": (
        "Generate videos through Dovie's managed video generation backend. "
        "Supports text-to-video, image-to-video, and first/last-frame video."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Video prompt.",
                "minLength": 1,
                "maxLength": 2000,
            },
            "image_url": {
                "type": "string",
                "description": "Optional first-frame HTTP(S) URL or local image path.",
            },
            "last_frame_image_url": {
                "type": "string",
                "description": "Optional last-frame HTTP(S) URL or local image path.",
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "1080p"],
                "default": "1080p",
            },
            "ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1", "landscape", "portrait", "square"],
                "default": "9:16",
            },
            "duration": {
                "type": "integer",
                "description": "Video duration in seconds.",
                "minimum": 1,
                "maximum": 30,
                "default": 5,
            },
            "camera_fixed": {
                "type": "boolean",
                "description": "Whether to keep the camera fixed.",
                "default": False,
            },
            "generation_type": {
                "type": "string",
                "enum": ["t2v", "i2v", "flf"],
                "description": "Generation mode. Omit to infer from image inputs.",
            },
            "service_provider": {
                "type": "string",
                "enum": ["sora", "seedance", "vidu"],
                "description": "Optional Dovie video provider id. Omit for the backend default.",
            },
        },
        "required": ["prompt"],
    },
}


def _env_url(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _runtime_token() -> str:
    return str(os.getenv("DOVIE_LLM_RUNTIME_TOKEN") or "").strip()


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else default


def _bounded_timeout(*values: Any) -> int:
    candidates: list[int] = []
    for value in values:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            candidates.append(parsed)
    requested = candidates[0] if candidates else DOVIE_MEDIA_PROXY_DEFAULT_TIMEOUT_SECONDS
    return max(10, min(requested, DOVIE_MEDIA_PROXY_MAX_TIMEOUT_SECONDS))


def _request_timeout(remaining: float | int | None = None) -> int:
    configured = _env_int(
        "DOVIE_MEDIA_PROXY_REQUEST_TIMEOUT",
        DOVIE_MEDIA_PROXY_DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    if remaining is None:
        return max(5, configured)
    try:
        remaining_value = int(max(1, float(remaining)))
    except (TypeError, ValueError):
        remaining_value = configured
    return max(1, min(configured, remaining_value))


def _append_query(url: str, params: dict[str, Any]) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        query[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def _join_url_path(url: str, *parts: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    base_path = parsed.path.rstrip("/")
    suffix = "/".join(urllib.parse.quote(str(part).strip("/"), safe="") for part in parts)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{base_path}/{suffix}" if suffix else base_path,
            "",
            "",
        )
    )


def _decode_json_response(raw: bytes, status: int) -> dict[str, Any]:
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Dovie media proxy returned non-JSON response: HTTP {status}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Dovie media proxy returned invalid JSON response: HTTP {status}")
    if status < 200 or status >= 300:
        detail = result.get("detail")
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error")
        else:
            message = detail
        raise RuntimeError(str(message or result.get("message") or f"HTTP {status}"))
    return result


def _raise_if_interrupted(message: str = "Dovie media proxy request interrupted") -> None:
    try:
        from tools.interrupt import is_interrupted
    except Exception:
        return
    if is_interrupted():
        raise InterruptedError(message)


def _run_proxy_request_interruptibly(fn, *, message: str = "Dovie media proxy request interrupted") -> dict[str, Any]:
    from tools.interrupt import run_blocking_interruptibly

    return run_blocking_interruptibly(fn, interrupted_message=message)


def _sleep_interruptibly(seconds: float, *, message: str = "Dovie media proxy request interrupted") -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        _raise_if_interrupted(message)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _post_json(url: str, payload: dict[str, Any], *, token: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except TimeoutError as exc:
        raise RuntimeError(f"Dovie media proxy request timed out after {timeout}s") from exc
    except socket.timeout as exc:
        raise RuntimeError(f"Dovie media proxy request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dovie media proxy request failed: {exc}") from exc
    return _decode_json_response(raw, status)


def _path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _active_workspace_root() -> Path:
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return resolve_agent_cwd().expanduser().resolve()
    except Exception:
        return Path(os.getcwd()).resolve()


def _attachment_root() -> Path | None:
    configured = str(os.getenv("DOVIE_ATTACHMENT_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    runtime_root = str(os.getenv("DOVIE_RUNTIME_HOME") or "").strip()
    if runtime_root:
        return (Path(runtime_root).expanduser().resolve().parent / "attachments").resolve()
    return None


def _media_asset_proxy_url() -> str:
    configured = _env_url("DOVIE_MEDIA_ASSET_PROXY_URL")
    if configured:
        return configured
    image_proxy_url = _env_url("DOVIE_IMAGE_GENERATE_PROXY_URL")
    if not image_proxy_url:
        return ""
    parsed = urllib.parse.urlsplit(image_proxy_url)
    if not parsed.path.rstrip("/").endswith("/image-generate"):
        return ""
    base_path = parsed.path.rstrip("/")[: -len("/image-generate")]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{base_path}/media-assets/reference-image",
            "",
            "",
        )
    )


def _local_reference_path(value: str) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() in {"http", "https"}:
        return None
    if parsed.scheme.lower() == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("local reference image file URL must not contain a remote host")
        raw_path = urllib.request.url2pathname(parsed.path)
    elif parsed.scheme and not re.match(r"^[a-zA-Z]:[\\/]", text):
        raise ValueError("reference image must use HTTP(S) or a permitted local path")
    else:
        raw_path = text

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = _active_workspace_root() / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"local reference image does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise ValueError(f"local reference image is not a file: {candidate}")

    allowed_roots = [_active_workspace_root()]
    attachment_root = _attachment_root()
    if attachment_root is not None:
        allowed_roots.append(attachment_root)
    if not any(_path_is_inside(resolved, root) for root in allowed_roots):
        raise ValueError(
            "local reference image must be inside the active workspace or Dovie attachment store"
        )
    return resolved


def _local_reference_metadata(path: Path) -> tuple[str, str]:
    filename = path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    metadata_path = path.parent / "meta.json"
    attachment_root = _attachment_root()
    if path.name != "blob" or attachment_root is None:
        return filename, content_type
    if not _path_is_inside(path, attachment_root):
        return filename, content_type
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return filename, content_type
    if not isinstance(metadata, dict):
        return filename, content_type
    original_name = str(metadata.get("fileName") or "").strip()
    declared_type = str(metadata.get("mimeType") or "").strip().lower()
    if original_name:
        filename = os.path.basename(original_name)
    if declared_type.startswith("image/"):
        content_type = declared_type
    return filename, content_type


def _post_reference_image(
    url: str,
    path: Path,
    *,
    token: str,
    timeout: int,
) -> dict[str, Any]:
    max_bytes = _env_int(
        "DOVIE_MEDIA_REFERENCE_MAX_BYTES",
        DOVIE_MEDIA_REFERENCE_DEFAULT_MAX_BYTES,
    )
    size_bytes = path.stat().st_size
    if size_bytes > max_bytes:
        raise ValueError(
            f"local reference image exceeds the {max_bytes // (1024 * 1024)} MB limit"
        )
    filename, content_type = _local_reference_metadata(path)
    safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    boundary = f"dovie-{uuid.uuid4().hex}"
    body = b"".join(
        (
            f"--{boundary}\r\n".encode("ascii"),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{safe_filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode("ascii"),
        )
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except TimeoutError as exc:
        raise RuntimeError(f"Dovie reference image upload timed out after {timeout}s") from exc
    except socket.timeout as exc:
        raise RuntimeError(f"Dovie reference image upload timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dovie reference image upload failed: {exc}") from exc
    return _decode_json_response(raw, status)


def _normalize_reference_image(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    local_path = _local_reference_path(text)
    if local_path is None:
        return text
    url = _media_asset_proxy_url()
    token = _runtime_token()
    if not url:
        raise RuntimeError("DOVIE_MEDIA_ASSET_PROXY_URL is not configured.")
    if not token:
        raise RuntimeError("DOVIE_LLM_RUNTIME_TOKEN is not configured.")
    result = _run_proxy_request_interruptibly(
        lambda: _post_reference_image(
            url,
            local_path,
            token=token,
            timeout=_request_timeout(),
        ),
        message="Dovie reference image upload interrupted",
    )
    uploaded_url = str(result.get("url") or "").strip()
    if not uploaded_url.startswith(("http://", "https://")):
        raise RuntimeError("Dovie reference image upload returned no usable URL")
    return uploaded_url


def _normalize_reference_images(values: Any) -> list[str] | None:
    if not isinstance(values, list):
        return None
    normalized = [_normalize_reference_image(value) for value in values if str(value or "").strip()]
    return normalized or None


def _get_json(url: str, *, token: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except TimeoutError as exc:
        raise RuntimeError(f"Dovie media proxy status request timed out after {timeout}s") from exc
    except socket.timeout as exc:
        raise RuntimeError(f"Dovie media proxy status request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dovie media proxy status request failed: {exc}") from exc
    return _decode_json_response(raw, status)


def _media_task_failed_message(result: dict[str, Any]) -> str:
    message = result.get("error_message") or result.get("error") or result.get("message")
    return str(message or "media generation failed")


def _poll_proxy_task(base_url: str, task_id: str, *, token: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    status_url = _join_url_path(base_url, "tasks", task_id)
    poll_interval = _env_int(
        "DOVIE_MEDIA_PROXY_POLL_INTERVAL",
        DOVIE_MEDIA_PROXY_DEFAULT_POLL_INTERVAL_SECONDS,
    )
    last_result: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        _raise_if_interrupted()
        remaining = deadline - time.monotonic()
        result = _run_proxy_request_interruptibly(
            lambda: _get_json(status_url, token=token, timeout=_request_timeout(remaining)),
        )
        last_result = result
        status = str(result.get("status") or "").lower()
        if status == "completed":
            return result
        if status in {"failed", "cancelled", "error"}:
            raise RuntimeError(_media_task_failed_message(result))
        _sleep_interruptibly(min(max(1, poll_interval), max(1, int(remaining))))

    suffix = ""
    if last_result:
        suffix = f"; last_status={last_result.get('status')}"
    raise RuntimeError(f"Dovie media proxy task timed out after {timeout}s{suffix}")


def _proxy_result(env_name: str, payload: dict[str, Any]) -> str:
    url = _env_url(env_name)
    token = _runtime_token()
    if not url:
        return tool_error(f"{env_name} is not configured.")
    if not token:
        return tool_error("DOVIE_LLM_RUNTIME_TOKEN is not configured.")
    timeout = _bounded_timeout(
        payload.pop("timeout", None),
        _env_int("DOVIE_MEDIA_PROXY_TIMEOUT", DOVIE_MEDIA_PROXY_DEFAULT_TIMEOUT_SECONDS),
    )
    try:
        result = _run_proxy_request_interruptibly(
            lambda: _post_json(url, payload, token=token, timeout=timeout),
        )
    except Exception as exc:
        return tool_error(str(exc))
    return json.dumps(result, ensure_ascii=False)


def _async_media_proxy_result(env_name: str, payload: dict[str, Any]) -> str:
    url = _env_url(env_name)
    token = _runtime_token()
    if not url:
        return tool_error(f"{env_name} is not configured.")
    if not token:
        return tool_error("DOVIE_LLM_RUNTIME_TOKEN is not configured.")

    timeout = _bounded_timeout(
        payload.pop("timeout", None),
        _env_int("DOVIE_MEDIA_PROXY_TIMEOUT", DOVIE_MEDIA_PROXY_DEFAULT_TIMEOUT_SECONDS),
    )
    try:
        start_result = _run_proxy_request_interruptibly(
            lambda: _post_json(
                _append_query(url, {"wait": False}),
                payload,
                token=token,
                timeout=_request_timeout(),
            ),
        )
        status = str(start_result.get("status") or "").lower()
        task_id = str(start_result.get("task_id") or "").strip()
        if status in {"completed", "failed", "cancelled", "error"} or not task_id:
            return json.dumps(start_result, ensure_ascii=False)
        final_result = _poll_proxy_task(url, task_id, token=token, timeout=timeout)
    except Exception as exc:
        return tool_error(str(exc))
    return json.dumps(final_result, ensure_ascii=False)


def _normalize_image_aspect_ratio(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "landscape":
        return "16:9"
    if text == "portrait":
        return "9:16"
    if text == "square":
        return "1:1"
    return text


def _normalize_video_ratio(value: Any) -> str:
    text = str(value or "9:16").strip().lower()
    if text == "landscape":
        return "16:9"
    if text == "portrait":
        return "9:16"
    if text == "square":
        return "1:1"
    return text


def dovie_image_generate(args: dict[str, Any]) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return tool_error("prompt is required.")
    try:
        reference_urls = _normalize_reference_images(args.get("reference_image_urls"))
        reference_url = _normalize_reference_image(args.get("reference_image_url"))
    except Exception as exc:
        return tool_error(str(exc))
    model = str(args.get("model") or "").strip()
    generation_type = "i2i" if reference_url or reference_urls else "t2i"
    payload = {
        "prompt": prompt,
        "negative_prompt": args.get("negative_prompt"),
        "generation_type": generation_type,
        "reference_image_url": reference_url or None,
        "reference_image_urls": reference_urls or None,
        "timeout": args.get("timeout"),
    }
    aspect_ratio = _normalize_image_aspect_ratio(args.get("aspect_ratio"))
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if args.get("generate_num") is not None:
        payload["generate_num"] = args["generate_num"]
    quality = str(args.get("quality") or "").strip()
    if quality:
        payload["quality"] = quality
    if model:
        payload["model"] = model
    return _async_media_proxy_result("DOVIE_IMAGE_GENERATE_PROXY_URL", payload)


def dovie_video_generate(args: dict[str, Any]) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return tool_error("prompt is required.")
    try:
        image_url = _normalize_reference_image(args.get("image_url"))
        last_frame_image_url = _normalize_reference_image(args.get("last_frame_image_url"))
    except Exception as exc:
        return tool_error(str(exc))
    generation_type = str(args.get("generation_type") or "").strip()
    if not generation_type:
        generation_type = "flf" if image_url and last_frame_image_url else "i2v" if image_url else "t2v"
    payload = {
        "prompt": prompt,
        "image_url": image_url or None,
        "last_frame_image_url": last_frame_image_url or None,
        "resolution": args.get("resolution") or "1080p",
        "ratio": _normalize_video_ratio(args.get("ratio")),
        "duration": args.get("duration", 5),
        "camera_fixed": args.get("camera_fixed", False),
        "generation_type": generation_type,
        "service_provider": args.get("service_provider") or "seedance",
        "timeout": args.get("timeout"),
    }
    return _async_media_proxy_result("DOVIE_VIDEO_GENERATE_PROXY_URL", payload)


registry.register(
    name="dovie_image_generate",
    toolset="dovie_image",
    schema=DOVIE_IMAGE_GENERATE_SCHEMA,
    handler=lambda args, **kw: dovie_image_generate(args),
    emoji="🎨",
    max_result_size_chars=80_000,
)

registry.register(
    name="dovie_video_generate",
    toolset="dovie_video",
    schema=DOVIE_VIDEO_GENERATE_SCHEMA,
    handler=lambda args, **kw: dovie_video_generate(args),
    emoji="🎬",
    max_result_size_chars=80_000,
)
