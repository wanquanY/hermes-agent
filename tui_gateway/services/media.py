from __future__ import annotations

import asyncio
import json
from pathlib import Path


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


def analyze_image_for_prompt(image_path: str) -> str:
    from tools.vision_tools import vision_analyze_tool

    analysis_prompt = (
        "Describe everything visible in this image in thorough detail. "
        "Include any text, code, data, objects, people, layout, colors, "
        "and any other notable visual information."
    )
    result_json = asyncio.run(
        vision_analyze_tool(image_url=str(image_path), user_prompt=analysis_prompt)
    )
    result = json.loads(result_json)
    analysis = str(result.get("analysis") or "").strip()
    if result.get("success") and analysis:
        return analysis
    error = str(result.get("error") or "").strip()
    if analysis and error:
        return f"Image analysis failed: {analysis} ({error})"
    if analysis:
        return f"Image analysis failed: {analysis}"
    if error:
        return f"Image analysis failed: {error}"
    return ""


def enrich_with_attached_images(user_text: str, image_paths: list[str]) -> str:
    parts: list[str] = []
    for raw in image_paths:
        path = Path(raw)
        try:
            desc = analyze_image_for_prompt(str(path))
            hint = f"Path: {path}"
            parts.append(
                f"[The user attached an image:\n{desc}]\n{hint}"
                if desc
                else f"[The user attached an image but analysis failed.]\n{hint}"
            )
        except Exception:
            parts.append(f"[The user attached an image but analysis failed.]\nPath: {path}")

    text = user_text or ""
    prefix = "\n\n".join(parts)
    if prefix:
        return f"{prefix}\n\n{text}" if text else prefix
    return text or "What do you see in this image?"
