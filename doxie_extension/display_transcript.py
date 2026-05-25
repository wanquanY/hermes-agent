"""Doxie-facing transcript display normalization.

These helpers sanitize Hermes-owned runtime instructions only at the Gateway
display boundary. They must not be used before agent execution, because the
agent still needs those instructions for correct cron delivery behavior.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_CRON_DELIVERY_GUIDANCE_RE = re.compile(
    r"""
    \A\s*
    \[IMPORTANT:\s+You\s+are\s+running\s+as\s+a\s+scheduled\s+cron\s+job\.
    .*?
    Never\s+combine\s+\[SILENT\]\s+with\s+content\s+.*?
    or\s+say\s+\[SILENT\]\s+and\s+nothing\s+more\.\]
    \s*
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

_TURN_METADATA_KEYS = ("turn_id", "run_id", "client_message_id")


def _turn_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: str(value.get(key) or "").strip()
        for key in _TURN_METADATA_KEYS
        if str(value.get(key) or "").strip()
    }


def _merge_turn_metadata(message: dict[str, Any], turn_metadata: dict[str, Any]) -> dict[str, Any]:
    if not turn_metadata:
        return message
    existing = message.get("metadata")
    metadata = dict(existing) if isinstance(existing, Mapping) else {}
    changed = False
    for key, value in turn_metadata.items():
        if not str(metadata.get(key) or "").strip():
            metadata[key] = value
            changed = True
    if changed:
        message["metadata"] = metadata
    return message


def sanitize_display_text(value: Any) -> str:
    """Remove Hermes internal prompt guidance from user-visible text."""

    text = str(value or "")
    if not text:
        return ""
    return _CRON_DELIVERY_GUIDANCE_RE.sub("", text, count=1).strip()


def sanitize_transcript_message(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a Doxie-display copy of a transcript message.

    The raw transcript message remains unchanged. Only user messages can contain
    the cron delivery guidance because Hermes prepends it to the job prompt.
    """

    item = dict(message)
    if item.get("role") != "user":
        return item

    raw_text = item.get("text")
    cleaned = sanitize_display_text(raw_text)
    if cleaned == str(raw_text or ""):
        return item
    if not cleaned:
        return None
    item["text"] = cleaned
    return item


def sanitize_transcript_messages(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    current_turn_metadata: dict[str, Any] = {}
    for message in messages:
        item = sanitize_transcript_message(message)
        if item is not None:
            role = str(item.get("role") or "")
            if role == "user":
                current_turn_metadata = _turn_metadata(item.get("metadata"))
            elif role in {"assistant", "tool"}:
                item = _merge_turn_metadata(item, current_turn_metadata)
            cleaned.append(item)
    return cleaned


def sanitize_session_list_item(session: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize session list title/preview for Doxie history surfaces."""

    item = dict(session)
    title = sanitize_display_text(item.get("title"))
    preview = sanitize_display_text(item.get("preview"))
    if title:
        item["title"] = title
    elif preview:
        item["title"] = preview
    else:
        item["title"] = ""
    item["preview"] = preview
    return item
