"""Prompt shaping for Doxie document attachments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".tsv",
    ".txt", ".md", ".markdown", ".json", ".xml", ".yaml", ".yml", ".toml",
    ".ini", ".log", ".rtf",
}
IMAGE_MIME_PREFIX = "image/"


def _attachment_name(item: dict[str, Any]) -> str:
    return str(item.get("fileName") or item.get("name") or item.get("id") or "attachment").strip()


def _attachment_path(item: dict[str, Any]) -> str:
    return str(item.get("path") or item.get("localPath") or item.get("local_path") or "").strip()


def _attachment_url(item: dict[str, Any]) -> str:
    return str(item.get("fileUrl") or item.get("file_url") or item.get("remoteUrl") or "").strip()


def _attachment_mime(item: dict[str, Any]) -> str:
    return str(item.get("mimeType") or item.get("mime_type") or "").strip()


def _attachment_file_type(item: dict[str, Any], path: str, url: str, name: str) -> str:
    explicit = str(item.get("fileType") or item.get("file_type") or "").strip().lower().lstrip(".")
    if explicit:
        return explicit
    for candidate in (path, url, name):
        suffix = Path(candidate).suffix.lower().lstrip(".")
        if suffix:
            return suffix
    mime = _attachment_mime(item).lower()
    if "/" in mime:
        return mime.rsplit("/", 1)[-1].split(";")[0]
    return ""


def is_document_attachment(item: dict[str, Any]) -> bool:
    mime = _attachment_mime(item).lower()
    if mime.startswith(IMAGE_MIME_PREFIX):
        return False
    name = _attachment_name(item)
    path = _attachment_path(item)
    url = _attachment_url(item)
    ext = f".{_attachment_file_type(item, path, url, name)}" if _attachment_file_type(item, path, url, name) else ""
    return ext in DOCUMENT_EXTENSIONS or mime.startswith("text/") or mime in {
        "application/pdf",
        "application/json",
        "application/xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }


def format_document_attachment_context(attachments: list[dict[str, Any]] | None) -> str:
    if not attachments:
        return ""
    rows: list[str] = []
    for index, item in enumerate(attachments, 1):
        if not isinstance(item, dict) or not is_document_attachment(item):
            continue
        name = _attachment_name(item)
        path = _attachment_path(item)
        url = _attachment_url(item)
        mime = _attachment_mime(item)
        file_type = _attachment_file_type(item, path, url, name)
        parse_args: list[str] = []
        if path:
            parse_args.append(f"path={path!r}")
        if url:
            parse_args.append(f"file_url={url!r}")
        if file_type:
            parse_args.append(f"file_type={file_type!r}")
        if mime:
            parse_args.append(f"mime_type={mime!r}")
        rows.append(
            f"{index}. {name}"
            + (f" ({mime})" if mime else "")
            + "\n"
            + f"   parse_document({', '.join(parse_args)})"
        )
    if not rows:
        return ""
    return (
        "[Doxie attached documents]\n"
        "The user attached document files. For any content-specific request, "
        "call parse_document with the matching attachment arguments before answering.\n"
        + "\n".join(rows)
        + "\n[/Doxie attached documents]"
    )


def enrich_prompt_with_document_attachments(prompt: Any, attachments: list[dict[str, Any]] | None) -> Any:
    if not isinstance(prompt, str):
        return prompt
    context = format_document_attachment_context(attachments)
    if not context:
        return prompt
    return f"{context}\n\n{prompt}" if prompt else context
