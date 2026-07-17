"""Lossless, bounded projection of MCP result content into Hermes values."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from tools.mcp_identity import canonical_mcp_tool_name

logger = logging.getLogger(__name__)

MAX_MCP_BLOCK_BYTES = 50 * 1024 * 1024
MAX_MCP_RESULT_CHARS = 4 * 1024 * 1024


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    try:
        concrete = vars(obj)
    except TypeError:
        concrete = {}
    for name in names:
        if name in concrete:
            return concrete[name]
    # unittest.mock manufactures arbitrary attributes on access. Only its
    # explicitly assigned ``__dict__`` values are protocol facts.
    if type(obj).__module__.startswith("unittest.mock"):
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _mime_type(obj: Any) -> str:
    return str(_value(obj, "mimeType", "mime_type", default="") or "").split(";", 1)[0].strip().lower()


def _safe_filename(*, uri: str, name: str, mime_type: str) -> str:
    candidate = Path(unquote(urlparse(uri).path)).name or Path(name or "").name
    if not candidate:
        candidate = "resource"
    if not Path(candidate).suffix:
        candidate += mimetypes.guess_extension(mime_type) or ".bin"
    return candidate


def _decode_base64(data: Any, *, max_bytes: int) -> bytes:
    if not isinstance(data, (str, bytes)):
        raise ValueError("payload is not base64 text")
    encoded = data.encode("ascii") if isinstance(data, str) else data
    max_encoded = ((max_bytes + 2) // 3) * 4
    if len(encoded) > max_encoded:
        raise ValueError(f"payload exceeds {max_bytes} bytes")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise ValueError("payload is invalid base64") from exc
    if len(decoded) > max_bytes:
        raise ValueError(f"payload exceeds {max_bytes} bytes")
    return decoded


def _cache_bytes(
    data: bytes,
    *,
    uri: str,
    name: str,
    mime_type: str,
    default_kind: str,
) -> str:
    from channels.platforms.base import cache_media_bytes

    cached = cache_media_bytes(
        data,
        filename=_safe_filename(uri=uri, name=name, mime_type=mime_type),
        mime_type=mime_type or None,
        default_kind=default_kind,
    )
    return f"MEDIA:{cached.path}" if cached is not None else ""


def _resource_header(label: str, *, uri: str, name: str, mime_type: str) -> str:
    fields = [label]
    if name:
        fields.append(f"name={name}")
    if uri:
        fields.append(f"uri={uri}")
    if mime_type:
        fields.append(f"mime={mime_type}")
    return "[" + "; ".join(fields) + "]"


def normalize_mcp_content_blocks(
    blocks: Iterable[Any] | None,
    *,
    server_name: str,
    max_block_bytes: int = MAX_MCP_BLOCK_BYTES,
) -> list[str]:
    """Project every known MCP content block without fetching remote links."""

    parts: list[str] = []
    for block in blocks or []:
        block_type = str(_value(block, "type", default="") or "").lower()
        direct_uri = str(_value(block, "uri", default="") or "")
        direct_text = _value(block, "text")
        direct_blob = _value(block, "blob")
        resource = _value(block, "resource")
        if (
            block_type in {"resource", "embedded_resource", "embeddedresource"}
            or resource is not None
            or (
                direct_uri
                and block_type not in {"resource_link", "resourcelink"}
                and (direct_text is not None or direct_blob is not None)
            )
        ):
            resource = resource if resource is not None else block
            uri = str(_value(resource, "uri", default="") or "")
            name = str(_value(resource, "name", default="") or "")
            mime_type = _mime_type(resource)
            header = _resource_header("Embedded MCP resource", uri=uri, name=name, mime_type=mime_type)
            resource_text = _value(resource, "text")
            if resource_text is not None:
                parts.append(f"{header}\n{resource_text}")
                continue
            blob = _value(resource, "blob", "data")
            try:
                raw = _decode_base64(blob, max_bytes=max_block_bytes)
                media = _cache_bytes(
                    raw,
                    uri=uri,
                    name=name,
                    mime_type=mime_type,
                    default_kind="document",
                )
                parts.append(f"{header}\n{media}" if media else header)
            except (ImportError, OSError, ValueError) as exc:
                logger.warning("MCP embedded resource omitted: %s", exc)
                parts.append(f"{header}\n[MCP resource omitted: {exc}]")
            continue

        if block_type == "text" or direct_text is not None:
            if direct_text:
                parts.append(str(direct_text))
            continue

        uri = direct_uri
        if block_type in {"resource_link", "resourcelink"} or uri:
            name = str(_value(block, "name", "title", default="") or "")
            mime_type = _mime_type(block)
            description = str(_value(block, "description", default="") or "")
            size = _value(block, "size")
            header = _resource_header("MCP resource link", uri=uri, name=name, mime_type=mime_type)
            details = [header]
            if description:
                details.append(description)
            if size is not None:
                details.append(f"size={size}")
            details.append(
                "Read with " + canonical_mcp_tool_name(server_name, "read_resource") + "; URI was not fetched automatically."
            )
            parts.append("\n".join(details))
            continue

        data = _value(block, "data")
        mime_type = _mime_type(block)
        if block_type in {"image", "audio"} or (
            data is not None
            and (mime_type.startswith("image/") or mime_type.startswith("audio/"))
        ):
            kind = "audio" if block_type == "audio" or mime_type.startswith("audio/") else "image"
            try:
                raw = _decode_base64(data, max_bytes=max_block_bytes)
                media = _cache_bytes(
                    raw,
                    uri="",
                    name=f"mcp-{kind}",
                    mime_type=mime_type,
                    default_kind=kind,
                )
                if media:
                    parts.append(media)
            except (ImportError, OSError, ValueError) as exc:
                logger.warning("MCP %s block omitted: %s", kind, exc)
                parts.append(f"[MCP {kind} omitted: {exc}]")
            continue

        parts.append(f"[Unsupported MCP content block: {block_type or type(block).__name__}]")
    return parts


def normalize_call_tool_result(
    result: Any,
    *,
    server_name: str,
    max_result_chars: int = MAX_MCP_RESULT_CHARS,
) -> dict[str, Any]:
    """Return the canonical JSON-compatible Hermes result envelope."""

    parts = normalize_mcp_content_blocks(
        _value(result, "content", default=[]) or [],
        server_name=server_name,
    )
    content = "\n".join(part for part in parts if part)
    if len(content) > max_result_chars:
        content = content[:max_result_chars] + "\n[MCP result truncated at canonical content boundary]"
    structured = _value(result, "structuredContent", "structured_content")
    is_error = bool(_value(result, "isError", "is_error", default=False))

    if is_error:
        envelope: dict[str, Any] = {"error": content or "MCP tool returned an error"}
        if structured is not None:
            envelope["structuredContent"] = structured
        return envelope
    if structured is not None and content:
        return {"result": content, "structuredContent": structured}
    if structured is not None:
        return {"result": structured}
    return {"result": content}


def dumps_call_tool_result(result: Any, *, server_name: str) -> str:
    return json.dumps(
        normalize_call_tool_result(result, server_name=server_name),
        ensure_ascii=False,
    )
