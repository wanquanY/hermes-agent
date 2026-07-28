from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def estimate_image_tokens(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        return 0
    return max(1, (width + 511) // 512) * max(1, (height + 511) // 512) * 85


def image_meta(path: Path) -> dict:
    meta = {"name": path.name}
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
        meta["width"] = int(width)
        meta["height"] = int(height)
        meta["token_estimate"] = estimate_image_tokens(int(width), int(height))
    except Exception:
        pass
    return meta


def _is_remote_image_ref(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def image_refs_for_prompt(submitted_images: list[str], prompt_text: Any) -> tuple[list[str], list[str]]:
    image_paths: list[str] = []
    seen_paths: set[str] = set()
    for raw in submitted_images or []:
        path = str(raw or "").strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        image_paths.append(path)

    image_urls: list[str] = []
    seen_urls: set[str] = set()
    try:
        from agent.image_routing import extract_image_refs

        extra_paths, extra_urls = extract_image_refs(str(prompt_text or ""))
    except Exception:
        return image_paths, image_urls

    for path in extra_paths:
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        image_paths.append(path)
    for url in extra_urls:
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        image_urls.append(url)
    return image_paths, image_urls


def enrich_with_attached_images(user_text: str, image_refs: list[str]) -> str:
    refs: list[str] = []
    seen_refs: set[str] = set()
    for raw_ref in image_refs or []:
        ref = str(raw_ref or "").strip()
        if not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        refs.append(ref)

    image_lines: list[str] = []
    for index, ref in enumerate(refs, 1):
        location = f"URL: {ref}" if _is_remote_image_ref(ref) else f"Path: {Path(ref)}"
        image_lines.append(
            f"{index}. {location}\n"
            f"   Required tool call: vision_analyze(image_url={ref!r}, question=<user request>)"
        )

    text = user_text or ""
    prefix = ""
    if image_lines:
        prefix = "\n".join([
            "[The user attached image files]",
            "Image attachment handling:",
            "- To answer anything about image contents, call vision_analyze with the exact image_url shown below before answering.",
            "- Do not infer visual contents from the file name, path, or URL.",
            "- If the user's request is a summary, description, OCR, comparison, or visual question, call vision_analyze first.",
            *image_lines,
        ])
    if prefix:
        return f"{prefix}\n\n{text}" if text else prefix
    return text or "What do you see in this image?"
