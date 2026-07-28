from __future__ import annotations

import hashlib
from typing import Any


PRIMARY_DELIVERABLE_TEXT_KEYS = (
    "delta",
    "text",
    "output",
    "content",
    "final_response",
    "finalResponse",
    "summary",
)


def primary_deliverable_text(payload: dict[str, Any] | None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    for key in PRIMARY_DELIVERABLE_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    message = payload.get("message")
    if isinstance(message, dict):
        for key in ("content", "text", "output"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def text_sha256(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def terminal_text_metadata(value: Any, *, prefix: str = "text") -> dict[str, Any]:
    text = str(value or "")
    result = {
        f"{prefix}_length": len(text),
        f"{prefix}_available": bool(text),
    }
    digest = text_sha256(text)
    if digest:
        result[f"{prefix}_sha256"] = digest
    return result
