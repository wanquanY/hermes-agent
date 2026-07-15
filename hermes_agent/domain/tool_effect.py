"""Canonical persistence codec for uncertain tool execution effects."""

from __future__ import annotations

from typing import Any, Mapping


TOOL_EFFECT_METADATA_KEY = "_hermes_tool_effect_disposition"
VALID_TOOL_EFFECT_DISPOSITIONS = frozenset({"none", "unknown"})


def normalize_tool_effect_disposition(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_TOOL_EFFECT_DISPOSITIONS else ""


def persist_tool_effect(
    metadata: Mapping[str, Any] | None,
    effect_disposition: Any,
) -> dict[str, Any]:
    """Return copied metadata containing only a valid canonical disposition."""
    result = dict(metadata or {})
    normalized = normalize_tool_effect_disposition(effect_disposition)
    if normalized:
        result[TOOL_EFFECT_METADATA_KEY] = normalized
    else:
        result.pop(TOOL_EFFECT_METADATA_KEY, None)
    return result


def tool_effect_from_metadata(metadata: Any) -> str:
    if not isinstance(metadata, Mapping):
        return ""
    return normalize_tool_effect_disposition(metadata.get(TOOL_EFFECT_METADATA_KEY))


__all__ = [
    "TOOL_EFFECT_METADATA_KEY",
    "VALID_TOOL_EFFECT_DISPOSITIONS",
    "normalize_tool_effect_disposition",
    "persist_tool_effect",
    "tool_effect_from_metadata",
]
