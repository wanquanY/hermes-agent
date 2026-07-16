"""Whole-turn response predicates owned by the live gateway boundary.

Control markers remain part of the model transcript, but they are never
user-visible output.  Streaming and non-streaming delivery share these exact
predicates so one path cannot drift from the other.
"""

from __future__ import annotations

from typing import Any


SILENT_REPLY_TOKEN = "NO_REPLY"
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
})


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def is_intentional_silence_response(response: Any) -> bool:
    """Return whether the entire response is exactly a silence marker."""

    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped or len(stripped) > 64:
        return False
    return _canonical_silence_candidate(stripped) in LIVE_GATEWAY_SILENT_MARKERS


def is_intentional_silence_agent_result(
    agent_result: dict | None,
    response: Any,
) -> bool:
    """Recognize control markers only for successful completed turns."""

    return (
        isinstance(agent_result, dict)
        and not agent_result.get("failed")
        and is_intentional_silence_response(response)
    )


def is_partial_silence_marker(text: Any) -> bool:
    """Return whether a streaming prefix could still become a marker."""

    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    candidate = _canonical_silence_candidate(stripped)
    return bool(candidate) and any(
        marker.startswith(candidate) for marker in LIVE_GATEWAY_SILENT_MARKERS
    )
