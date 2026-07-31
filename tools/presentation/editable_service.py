"""Controlled SlideSpec-to-PPTX orchestration for Dovie Desktop."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import AbstractSet, Any, Callable

from tools.presentation.slidespec_contract import (
    CHART_DATA_KEYS,
    CHART_KINDS,
    CHART_SERIES_KEYS,
    ELEMENT_KEYS,
    ELEMENT_TYPES,
    IMAGE_FITS,
    PAGE_CONTENT_KEYS,
    PAGE_KEYS,
    PAGE_LAYOUTS,
    PAGE_ITEM_KEYS,
    PAGE_METRIC_KEYS,
    PAGE_SIZES,
    PAGE_STEP_KEYS,
    SHAPE_KINDS,
    SLIDESPEC_VERSION,
    SPEC_KEYS,
    TEXT_RUN_KEYS,
    THEME_KEYS,
    THEME_PALETTE_TOKENS,
    THEME_PRESETS,
    THEME_SCALE_TOKENS,
)
from tools.presentation.reference_deck import (
    apply_reference_style,
    inspect_reference_deck,
)

MAX_PRESENTATION_PAGES = 40
MAX_SLIDESPEC_BYTES = 2 * 1024 * 1024
DEFAULT_RENDER_TIMEOUT_SECONDS = 180
PRESENTATION_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)


def _default_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted
    except Exception:
        return False
    return bool(is_interrupted())


def _safe_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    forbidden = '<>:"/\\|?*'
    sanitized = "".join("-" if char in forbidden or ord(char) < 32 else char for char in normalized)
    sanitized = "-".join(sanitized.split())
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    return (sanitized.strip("-. ") or "presentation")[:80]


def _is_inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _workspace_path(
    value: str,
    *,
    workspace_root: Path,
    label: str,
    suffix: str | None = None,
) -> Path:
    raw = str(value or "").strip()
    candidate = Path(raw) if raw else workspace_root
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    if not _is_inside(resolved, workspace_root):
        raise ValueError(f"{label} must stay inside the active workspace")
    if suffix and resolved.suffix.lower() != suffix:
        raise ValueError(f"{label} must end with {suffix}")
    return resolved


def _attachment_root() -> Path | None:
    value = str(os.environ.get("DOVIE_ATTACHMENT_ROOT") or "").strip()
    return Path(value).resolve() if value else None


def _resolve_asset_path(value: str, *, workspace_root: Path) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("image src cannot be empty")
    if raw.lower().startswith(("http://", "https://", "data:")):
        raise ValueError("presentation image src must be a local workspace or attachment path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    allowed_roots = [workspace_root]
    attachment_root = _attachment_root()
    if attachment_root:
        allowed_roots.append(attachment_root)
    if not any(_is_inside(resolved, root) for root in allowed_roots):
        raise ValueError("presentation image src escapes the workspace and attachment boundaries")
    if not resolved.is_file():
        raise ValueError(f"presentation image not found: {resolved}")
    return str(resolved)

def _resolve_reference_deck_path(value: str, *, workspace_root: Path) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("reference_deck_path cannot be empty")
    if raw.lower().startswith(("http://", "https://", "data:")):
        raise ValueError("reference_deck_path must be a local workspace or attachment path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    allowed_roots = [workspace_root]
    attachment_root = _attachment_root()
    if attachment_root:
        allowed_roots.append(attachment_root)
    if not any(_is_inside(resolved, root) for root in allowed_roots):
        raise ValueError("reference_deck_path escapes workspace and attachment boundaries")
    if not resolved.is_file() or resolved.suffix.lower() != ".pptx":
        raise ValueError("reference_deck_path must be an existing .pptx file")
    return str(resolved)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{label} must be finite")
    return result


def _reject_unknown_keys(
    value: dict[str, Any],
    allowed: AbstractSet[str],
    label: str,
) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        migration_hints = {
            "fills": "replace fills with one fill or fill_token value",
            "typography": (
                "flatten typography into style, font_size, color/color_token, "
                "bold, italic, align, and valign"
            ),
        }
        hints = [
            migration_hints[field]
            for field in unknown
            if field in migration_hints
        ]
        suffix = f"; {'; '.join(hints)}" if hints else ""
        raise ValueError(
            f"{label} contains unsupported fields: {', '.join(unknown)}{suffix}"
        )


def _validate_box(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} must contain [x, y, width, height]")
    box = [_number(item, label) for item in value]
    x, y, width, height = box
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise ValueError(f"{label} must stay inside the normalized 0..1 page")
    return box


def _validate_chart(element: dict[str, Any], label: str) -> None:
    kind = str(element.get("kind") or "bar")
    if kind not in CHART_KINDS:
        raise ValueError(f"{label}.kind is unsupported")
    data = element.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{label}.data must be an object")
    _reject_unknown_keys(data, CHART_DATA_KEYS, f"{label}.data")
    labels = data.get("labels")
    series = data.get("series")
    if not isinstance(labels, list) or not labels:
        raise ValueError(f"{label}.data.labels must be a non-empty array")
    if not isinstance(series, list) or not series:
        raise ValueError(f"{label}.data.series must be a non-empty array")
    for series_index, item in enumerate(series):
        if isinstance(item, dict):
            _reject_unknown_keys(
                item,
                CHART_SERIES_KEYS,
                f"{label}.data.series[{series_index}]",
            )
        values = item.get("values") if isinstance(item, dict) else None
        if not isinstance(values, list) or len(values) != len(labels):
            raise ValueError(
                f"{label}.data.series[{series_index}].values must match labels"
            )
        for value_index, value in enumerate(values):
            _number(value, f"{label}.data.series[{series_index}].values[{value_index}]")


def _validate_theme(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("slidespec.theme must be an object")
    _reject_unknown_keys(value, THEME_KEYS, "slidespec.theme")
    preset = str(value.get("preset") or value.get("palette_ref") or "dovie-default")
    preset = preset.removeprefix("asset://theme/")
    if preset not in THEME_PRESETS:
        raise ValueError("slidespec.theme.preset is unsupported")
    palette = value.get("palette")
    if palette is not None and not isinstance(palette, dict):
        raise ValueError("slidespec.theme.palette must be an object")
    if isinstance(palette, dict):
        _reject_unknown_keys(
            palette,
            THEME_PALETTE_TOKENS,
            "slidespec.theme.palette",
        )
        for key, color in palette.items():
            if not isinstance(color, str) or not color.removeprefix("#").isalnum():
                raise ValueError(f"slidespec.theme.palette.{key} must be a hex color")
            normalized = color.removeprefix("#")
            if len(normalized) != 6 or any(
                character not in "0123456789abcdefABCDEF"
                for character in normalized
            ):
                raise ValueError(f"slidespec.theme.palette.{key} must be a hex color")
    scale = value.get("scale")
    if scale is not None:
        if not isinstance(scale, dict):
            raise ValueError("slidespec.theme.scale must be an object")
        _reject_unknown_keys(
            scale,
            THEME_SCALE_TOKENS,
            "slidespec.theme.scale",
        )
        for key, size in scale.items():
            numeric_size = _number(size, f"slidespec.theme.scale.{key}")
            if numeric_size < 6 or numeric_size > 96:
                raise ValueError(
                    f"slidespec.theme.scale.{key} must be between 6 and 96"
                )


def _validate_page_metadata(page: dict[str, Any], page_index: int) -> None:
    _reject_unknown_keys(page, PAGE_KEYS, f"pages[{page_index}]")
    layout = str(page.get("layout") or "freeform")
    if layout not in PAGE_LAYOUTS:
        raise ValueError(f"pages[{page_index}].layout is unsupported")
    notes = page.get("speaker_notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError(f"pages[{page_index}].speaker_notes must be a string")
    sources = page.get("sources")
    if sources is None:
        return
    if not isinstance(sources, list):
        raise ValueError(f"pages[{page_index}].sources must be an array")
    for source_index, source in enumerate(sources):
        if isinstance(source, str):
            continue
        if not isinstance(source, dict):
            raise ValueError(
                f"pages[{page_index}].sources[{source_index}] must be a string or object"
            )
        if not any(
            str(source.get(key) or "").strip()
            for key in ("label", "uri", "locator")
        ):
            raise ValueError(
                f"pages[{page_index}].sources[{source_index}] cannot be empty"
            )


def _normalize_page_content(
    value: Any,
    *,
    page_index: int,
    workspace_root: Path,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"pages[{page_index}].content must be an object")
    content = deepcopy(value)
    label = f"pages[{page_index}].content"
    _reject_unknown_keys(content, PAGE_CONTENT_KEYS, label)
    image_src = str(content.get("image_src") or "").strip()
    if image_src:
        content["image_src"] = _resolve_asset_path(
            image_src,
            workspace_root=workspace_root,
        )
    image_fit = str(content.get("image_fit") or "cover")
    if image_fit not in IMAGE_FITS:
        raise ValueError(
            f"{label}.image_fit must be cover or contain; stretch is forbidden"
        )
    for collection_key, allowed_keys in (
        ("metrics", PAGE_METRIC_KEYS),
        ("steps", PAGE_STEP_KEYS),
        ("items", PAGE_ITEM_KEYS),
    ):
        collection = content.get(collection_key)
        if collection is None:
            continue
        if not isinstance(collection, list):
            raise ValueError(f"{label}.{collection_key} must be an array")
        for item_index, item in enumerate(collection):
            if not isinstance(item, dict):
                raise ValueError(
                    f"{label}.{collection_key}[{item_index}] must be an object"
                )
            _reject_unknown_keys(
                item,
                allowed_keys,
                f"{label}.{collection_key}[{item_index}]",
            )
    bullets = content.get("bullets")
    if bullets is not None and (
        not isinstance(bullets, list)
        or any(not isinstance(item, str) for item in bullets)
    ):
        raise ValueError(f"{label}.bullets must be an array of strings")
    return content


def _normalize_element(
    value: Any,
    *,
    page_index: int,
    element_index: int,
    workspace_root: Path,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"pages[{page_index}].elements[{element_index}] must be an object")
    element = deepcopy(value)
    label = f"pages[{page_index}].elements[{element_index}]"
    element_type = str(element.get("type") or "")
    if element_type not in ELEMENT_TYPES:
        hint = (
            "; use type='shape' with kind='rect' for rectangles"
            if element_type in {"rectangle", "rect"}
            else ""
        )
        supported = ", ".join(sorted(ELEMENT_TYPES))
        raise ValueError(
            f"{label}.type is unsupported ({element_type!r}); "
            f"supported types: {supported}{hint}"
        )
    _reject_unknown_keys(element, ELEMENT_KEYS[element_type], label)
    element["type"] = element_type
    element["box"] = _validate_box(element.get("box"), f"{label}.box")
    if element_type == "text":
        runs = element.get("runs")
        if runs is None and not isinstance(element.get("text"), str):
            raise ValueError(f"{label} requires text or runs")
        if runs is not None and (
            not isinstance(runs, list)
            or any(not isinstance(run, dict) or not isinstance(run.get("text"), str) for run in runs)
        ):
            raise ValueError(f"{label}.runs must be an array of text runs")
        if isinstance(runs, list):
            for run_index, run in enumerate(runs):
                _reject_unknown_keys(
                    run,
                    TEXT_RUN_KEYS,
                    f"{label}.runs[{run_index}]",
                )
    elif element_type == "shape":
        if str(element.get("kind") or "rect") not in SHAPE_KINDS:
            raise ValueError(f"{label}.kind is unsupported")
    elif element_type == "image":
        image_fit = str(element.get("fit") or "contain")
        if image_fit not in IMAGE_FITS:
            raise ValueError(
                f"{label}.fit must be cover or contain; stretch is forbidden"
            )
        element["src"] = _resolve_asset_path(
            str(element.get("src") or ""),
            workspace_root=workspace_root,
        )
    elif element_type == "chart":
        _validate_chart(element, label)
    elif element_type == "table":
        rows = element.get("rows")
        if not isinstance(rows, list) or not rows or any(not isinstance(row, list) for row in rows):
            raise ValueError(f"{label}.rows must be a non-empty two-dimensional array")
        width = len(rows[0])
        if width == 0 or any(len(row) != width for row in rows):
            raise ValueError(f"{label}.rows must have a consistent non-zero column count")
    return element


def normalize_slidespec(value: Any, *, workspace_root: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("slidespec must be an object")
    root = Path(workspace_root).resolve()
    spec = deepcopy(value)
    _reject_unknown_keys(spec, SPEC_KEYS, "slidespec")
    if str(spec.get("version") or "") != SLIDESPEC_VERSION:
        raise ValueError(f"slidespec.version must be {SLIDESPEC_VERSION}")
    if str(spec.get("page_size") or "16:9") not in PAGE_SIZES:
        raise ValueError("slidespec.page_size must be one of 16:9, 4:3, or 1:1")
    _validate_theme(spec.get("theme"))
    pages = spec.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PRESENTATION_PAGES:
        raise ValueError(
            f"slidespec.pages must contain 1..{MAX_PRESENTATION_PAGES} pages"
        )
    normalized_pages: list[dict[str, Any]] = []
    seen_page_ids: set[str] = set()
    for page_index, value_page in enumerate(pages):
        if not isinstance(value_page, dict):
            raise ValueError(f"pages[{page_index}] must be an object")
        page = deepcopy(value_page)
        page_id = str(page.get("id") or f"p{page_index + 1}").strip()
        if not page_id or page_id in seen_page_ids:
            raise ValueError("every slidespec page id must be non-empty and unique")
        seen_page_ids.add(page_id)
        page["id"] = page_id
        _validate_page_metadata(page, page_index)
        elements = page.get("elements")
        content = page.get("content")
        if content is None and (not isinstance(elements, list) or not elements):
            raise ValueError(
                f"pages[{page_index}] requires content or non-empty elements"
            )
        if content is not None:
            page["content"] = _normalize_page_content(
                content,
                page_index=page_index,
                workspace_root=root,
            )
        if elements is not None:
            if not isinstance(elements, list) or not elements:
                raise ValueError(
                    f"pages[{page_index}].elements must be non-empty when provided"
                )
            page["elements"] = [
                _normalize_element(
                    element,
                    page_index=page_index,
                    element_index=element_index,
                    workspace_root=root,
                )
                for element_index, element in enumerate(elements)
            ]
        normalized_pages.append(page)
    spec["pages"] = normalized_pages
    encoded = json.dumps(spec, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_SLIDESPEC_BYTES:
        raise ValueError(f"slidespec exceeds {MAX_SLIDESPEC_BYTES} bytes")
    return spec


def _renderer_command() -> tuple[list[str], dict[str, str]]:
    node_binary = str(os.environ.get("DOVIE_NODE_BINARY") or "").strip()
    renderer_path = str(os.environ.get("DOVIE_DECK_RENDERER_PATH") or "").strip()
    if not node_binary or not Path(node_binary).is_file():
        raise RuntimeError("Dovie editable-deck Node runtime is unavailable")
    if not renderer_path or not Path(renderer_path).is_file():
        raise RuntimeError("Dovie editable-deck renderer is unavailable")
    env = dict(os.environ)
    if str(os.environ.get("DOVIE_NODE_RUN_AS_NODE") or "") == "1":
        env["ELECTRON_RUN_AS_NODE"] = "1"
    return [node_binary, renderer_path], env


def editable_renderer_available() -> bool:
    try:
        _renderer_command()
    except RuntimeError:
        return False
    return True


def _run_renderer(
    payload: dict[str, Any],
    *,
    interrupted: Callable[[], bool],
    timeout_seconds: int,
) -> dict[str, Any]:
    command, env = _renderer_command()
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    started_at = time.monotonic()
    try:
        if process.stdin is None:
            raise RuntimeError("editable-deck renderer stdin is unavailable")
        process.stdin.write(encoded)
        process.stdin.close()
        process.stdin = None
        while process.poll() is None:
            if interrupted():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise InterruptedError("editable presentation build interrupted")
            if time.monotonic() - started_at > timeout_seconds:
                process.kill()
                raise TimeoutError(
                    f"editable presentation build exceeded {timeout_seconds} seconds"
                )
            time.sleep(0.05)
        stdout, stderr = process.communicate()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"editable-deck renderer returned invalid JSON{f': {detail}' if detail else ''}"
        ) from exc
    if process.returncode != 0 or result.get("status") != "completed":
        message = str(result.get("error") or "").strip()
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or detail or "editable presentation build failed")
    return result


def _unique_output_path(destination: Path, overwrite: bool) -> Path:
    def is_available(candidate: Path) -> bool:
        spec_path = candidate.with_suffix(".slidespec.json")
        quality_path = candidate.with_name(
            f".{candidate.stem}.presentation.json"
        )
        return (
            not candidate.exists()
            and not spec_path.exists()
            and not quality_path.exists()
        )

    if overwrite or is_available(destination):
        return destination
    for index in range(2, 10_000):
        candidate = destination.with_name(f"{destination.stem}-{index}{destination.suffix}")
        if is_available(candidate):
            return candidate
    raise RuntimeError("could not allocate a unique presentation output path")


def _sha256(path_value: Path) -> str:
    hasher = hashlib.sha256()
    with path_value.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _render_previews(
    presentation_path: Path,
    *,
    interrupted: Callable[[], bool],
    preview_root: Path | None = None,
) -> dict[str, Any]:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    if not soffice or not pdftoppm:
        return {
            "status": "unavailable",
            "reason": "LibreOffice and Poppler are required for local preview rendering",
            "pdf_path": None,
            "page_images": [],
        }
    if interrupted():
        raise InterruptedError("editable presentation preview interrupted")
    resolved_preview_root = (
        preview_root.resolve()
        if preview_root is not None
        else presentation_path.with_name(f".{presentation_path.stem}.preview")
    )
    resolved_preview_root.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(
        tempfile.mkdtemp(
            prefix=f".{presentation_path.stem}-",
            dir=resolved_preview_root.parent,
        )
    )
    try:
        subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(stage_root),
                str(presentation_path),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        pdf_path = stage_root / f"{presentation_path.stem}.pdf"
        if not pdf_path.is_file():
            raise RuntimeError("LibreOffice completed without a PDF output")
        if interrupted():
            raise InterruptedError("editable presentation preview interrupted")
        subprocess.run(
            [
                pdftoppm,
                "-png",
                "-r",
                "150",
                str(pdf_path),
                str(stage_root / "page"),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        page_images = sorted(stage_root.glob("page-*.png"))
        if not page_images:
            raise RuntimeError("Poppler completed without page previews")
        if resolved_preview_root.exists():
            shutil.rmtree(resolved_preview_root)
        stage_root.replace(resolved_preview_root)
        return {
            "status": "completed",
            "reason": None,
            "pdf_path": str(resolved_preview_root / pdf_path.name),
            "page_images": [
                str(resolved_preview_root / image.name)
                for image in page_images
            ],
        }
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"editable presentation preview failed{f': {detail}' if detail else ''}"
        ) from exc
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)

def inspect_reference_presentation(
    args: dict[str, Any],
    *,
    workspace_root: str,
    interrupted: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    root = Path(workspace_root).resolve()
    interrupted_fn = interrupted or _default_interrupted
    reference_path = _resolve_reference_deck_path(
        str(args.get("presentation_path") or ""),
        workspace_root=root,
    )
    style = inspect_reference_deck(reference_path)
    preview = {
        "status": "skipped",
        "reason": "render_preview=false",
        "pdf_path": None,
        "page_images": [],
    }
    if args.get("render_preview", True) is not False:
        cache_key = hashlib.sha256(
            f"{reference_path}:{Path(reference_path).stat().st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:20]
        preview = _render_previews(
            Path(reference_path),
            interrupted=interrupted_fn,
            preview_root=root / ".dovie" / "presentation-references" / cache_key,
        )
    return {
        "dovie_event": "presentation_reference_inspected",
        "status": "completed",
        "message": (
            "Reference presentation inspected. Load every preview page into the "
            "current authoring model's native visual context before authoring, "
            "and pass presentation_path as reference_deck_path when building."
        ),
        "reference_deck": style.as_dict(),
        "preview": preview,
    }


def build_editable_presentation(
    args: dict[str, Any],
    *,
    workspace_root: str,
    interrupted: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    interrupted_fn = interrupted or _default_interrupted
    root = Path(workspace_root).resolve()
    raw_spec = args.get("slidespec")
    reference_style = None
    reference_deck_value = str(args.get("reference_deck_path") or "").strip()
    if reference_deck_value:
        reference_deck_path = _resolve_reference_deck_path(
            reference_deck_value,
            workspace_root=root,
        )
        reference_style = inspect_reference_deck(reference_deck_path)
        if not isinstance(raw_spec, dict):
            raise ValueError("slidespec must be an object")
        raw_spec = apply_reference_style(raw_spec, reference_style)
    spec = normalize_slidespec(raw_spec, workspace_root=str(root))
    title = str(args.get("title") or spec.get("title") or "Untitled presentation").strip()
    output_value = str(args.get("output_path") or "").strip()
    if not output_value:
        output_value = f"presentations/{_safe_slug(title)}.pptx"
    destination = _workspace_path(
        output_value,
        workspace_root=root,
        label="presentation output",
        suffix=".pptx",
    )
    destination_existed = destination.exists()
    destination = _unique_output_path(destination, bool(args.get("overwrite", False)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    timeout_seconds = int(
        args.get("timeout_seconds") or DEFAULT_RENDER_TIMEOUT_SECONDS
    )
    if timeout_seconds < 10 or timeout_seconds > 600:
        raise ValueError("timeout_seconds must be between 10 and 600")
    result = _run_renderer(
        {
            "output_path": str(destination),
            "overwrite": bool(args.get("overwrite", False)),
            "spec": spec,
        },
        interrupted=interrupted_fn,
        timeout_seconds=timeout_seconds,
    )
    preview = (
        _render_previews(destination, interrupted=interrupted_fn)
        if args.get("render_preview", True) is not False
        else {
            "status": "skipped",
            "reason": "render_preview=false",
            "pdf_path": None,
            "page_images": [],
        }
    )
    diagnostics = result.get("diagnostics")
    diagnostic_pages = diagnostics if isinstance(diagnostics, list) else []
    issue_count = sum(
        len(page.get("issues") or [])
        for page in diagnostic_pages
        if isinstance(page, dict) and isinstance(page.get("issues"), list)
    )
    structural_status = (
        "needs_revision"
        if issue_count > 0 or result.get("qa_status") == "needs_revision"
        else "passed"
    )
    visual_status = (
        "awaiting_agent_review"
        if preview.get("status") == "completed"
        else "unavailable"
        if preview.get("status") == "unavailable"
        else "skipped"
    )
    quality = {
        "status": structural_status,
        "structural_status": structural_status,
        "visual_status": visual_status,
        "issue_count": issue_count,
        "requirements": (
            "Repair every structural issue, load every rendered page with "
            "vision_analyze at readable size, and rebuild until clean. A "
            "vision-capable current authoring model must inspect the raw pixels "
            "itself; auxiliary vision analysis is the fallback only for a "
            "non-vision current model."
        ),
    }
    return {
        **result,
        "dovie_event": "presentation_build_completed",
        "message": (
            f"Built editable presentation with {issue_count} structural issue(s); "
            "revision is required before delivery."
            if structural_status == "needs_revision"
            else (
                "Built editable presentation. Inspect every rendered page before delivery."
                if visual_status == "awaiting_agent_review"
                else "Built editable presentation; local visual preview was not verified."
            )
        ),
        "mime_type": PRESENTATION_MIME_TYPE,
        "output_path": str(destination),
        "sha256": _sha256(destination),
        "preview": preview,
        "quality": quality,
        "reference_deck": (
            {
                **reference_style.as_dict(),
                "mode": "theme-and-page-style",
            }
            if reference_style is not None
            else None
        ),
        "artifacts": [
            {
                "path": str(destination),
                "title": title,
                "mime_type": PRESENTATION_MIME_TYPE,
                "operation": (
                    "modified"
                    if destination_existed and bool(args.get("overwrite", False))
                    else "created"
                ),
            }
        ],
    }
