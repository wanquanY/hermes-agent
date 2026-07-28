"""Canonical codec for persisted message content values."""

from __future__ import annotations

import json
from typing import Any

from hermes_agent.domain.text_safety import scrub_lone_surrogates

CONTENT_JSON_PREFIX = "\x00json:"


def encode_message_content(content: Any) -> Any:
    """Encode structured message content for SQLite storage."""

    if content is None or isinstance(content, (bytes, int, float)):
        return content
    if isinstance(content, str):
        return scrub_lone_surrogates(content)
    try:
        return CONTENT_JSON_PREFIX + json.dumps(
            scrub_lone_surrogates(content),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        return scrub_lone_surrogates(str(content))


def decode_message_content(content: Any, *, allow_legacy_json: bool = False) -> Any:
    """Decode content stored by :func:`encode_message_content`.

    ``allow_legacy_json`` is intentionally opt-in: many historical transcript
    rows contain JSON-looking text that is user-visible prose or tool output,
    not structured multimodal content.
    """

    if not isinstance(content, str):
        return content
    if content.startswith(CONTENT_JSON_PREFIX):
        try:
            return json.loads(content[len(CONTENT_JSON_PREFIX):])
        except (json.JSONDecodeError, TypeError):
            return content
    if allow_legacy_json and content[:1] in {"[", "{"}:
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
        return decoded if isinstance(decoded, (list, dict)) else content
    return content


__all__ = [
    "CONTENT_JSON_PREFIX",
    "decode_message_content",
    "encode_message_content",
]
