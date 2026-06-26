from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def submitted_attachments(params: dict[str, Any]) -> list[dict[str, Any]]:
    raw_attachments = params.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    attachments: list[dict[str, Any]] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("fileName") or raw.get("file_name") or "").strip()
        item = {
            "id": str(raw.get("id") or raw.get("fileId") or raw.get("file_id") or "").strip(),
            "name": name,
            "fileName": name,
            "mimeType": str(raw.get("mimeType") or raw.get("mime_type") or "").strip(),
            "size": raw.get("size") or raw.get("sizeBytes") or raw.get("size_bytes") or 0,
            "path": str(raw.get("path") or raw.get("localPath") or raw.get("local_path") or "").strip(),
            "fileUrl": str(raw.get("fileUrl") or raw.get("file_url") or raw.get("remoteUrl") or raw.get("remote_url") or "").strip(),
            "previewUrl": str(raw.get("previewUrl") or raw.get("preview_url") or raw.get("url") or "").strip(),
            "kind": str(raw.get("kind") or "").strip(),
        }
        attachments.append({
            key: value for key, value in item.items()
            if value is not None and not (isinstance(value, str) and value == "")
        })
    return attachments


def submitted_image_paths(params: dict[str, Any]) -> list[str]:
    raw_attachments = params.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    image_paths: list[str] = []
    image_extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
    }
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        raw_path = str(raw.get("path") or "").strip()
        if not raw_path:
            continue
        mime_type = str(raw.get("mimeType") or raw.get("mime_type") or "").lower()
        kind = str(raw.get("kind") or "").lower()
        path = Path(raw_path).expanduser()
        is_image = (
            kind == "image"
            or mime_type.startswith("image/")
            or path.suffix.lower() in image_extensions
        )
        if not is_image:
            continue
        if not path.is_file():
            print(
                f"[tui_gateway] prompt.submit skipped missing image attachment: {path}",
                file=sys.stderr,
                flush=True,
            )
            continue
        image_paths.append(str(path))
    return image_paths
