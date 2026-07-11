"""Pure stream-fragment identity and merge rules for run-event persistence."""

from __future__ import annotations

from typing import Any

from hermes_team_mission.runtime.run_event_retention import (
    COALESCIBLE_STREAM_EVENT_TYPES,
)


STREAM_COMPACTION_BOUNDARY_EVENT_TYPES = frozenset(
    {
        "message.start",
        "message.complete",
        "error",
        "session.interrupted",
    }
)
SUBAGENT_STREAM_COMPACTION_BOUNDARY_EVENT_TYPES = frozenset(
    {
        "subagent.start",
        "subagent.tool",
        "subagent.complete",
        "subagent.error",
    }
)
SUBAGENT_COALESCIBLE_STREAM_EVENT_TYPES = frozenset(
    {
        "subagent.output_delta",
        "subagent.reasoning_delta",
        "subagent.thinking",
    }
)
STREAM_IDENTITY_PAYLOAD_KEYS = (
    "subagent_id",
    "subagentId",
    "source",
    "role",
    "delegate_call_id",
    "delegateCallId",
    "tool_call_id",
    "toolCallId",
)


def is_coalescible_stream_delta(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "").strip()
    return event_type in COALESCIBLE_STREAM_EVENT_TYPES and bool(stream_text(event))


def stream_compaction_boundaries(event_type: str) -> frozenset[str]:
    boundaries = STREAM_COMPACTION_BOUNDARY_EVENT_TYPES
    if str(event_type or "").strip() in SUBAGENT_COALESCIBLE_STREAM_EVENT_TYPES:
        return boundaries | SUBAGENT_STREAM_COMPACTION_BOUNDARY_EVENT_TYPES
    return boundaries


def stream_events_can_coalesce(
    previous_event: dict[str, Any],
    event: dict[str, Any],
) -> bool:
    return (
        is_coalescible_stream_delta(previous_event)
        and is_coalescible_stream_delta(event)
        and stream_identity(previous_event) == stream_identity(event)
    )


def merge_stream_payload(
    previous_event: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    previous_payload = _payload(previous_event)
    payload = _payload(event)
    merged_payload = dict(previous_payload)
    merged_text = merge_stream_text(stream_text(previous_event), stream_text(event))
    for key in ("text", "delta", "output"):
        if key in previous_payload or key in payload:
            merged_payload[key] = merged_text
    merged_payload.pop("rendered", None)
    return merged_payload


def stream_identity(event: dict[str, Any]) -> tuple[Any, ...]:
    payload = _payload(event)
    identity: list[Any] = [
        str(event.get("type") or "").strip(),
        _event_text(event, payload, "run_id", "runId"),
        _event_text(event, payload, "turn_id", "turnId"),
        _event_text(event, payload, "runtime_scope_key", "runtimeScopeKey"),
        str(payload.get("mode") or "").strip().lower(),
    ]
    identity.extend(str(payload.get(key) or "").strip() for key in STREAM_IDENTITY_PAYLOAD_KEYS)
    return tuple(identity)


def stream_text(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return str(payload.get("text") or payload.get("delta") or payload.get("output") or "")


def merge_stream_text(previous_text: str, incoming_text: str) -> str:
    previous = str(previous_text or "")
    incoming = str(incoming_text or "")
    if not incoming:
        return previous
    if not previous:
        return incoming
    if incoming == previous or incoming in previous:
        return previous
    if incoming.startswith(previous):
        return incoming
    overlap = _suffix_prefix_overlap(previous, incoming)
    return previous + incoming[overlap:] if overlap > 0 else previous + incoming


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _event_text(
    event: dict[str, Any],
    payload: dict[str, Any],
    snake_key: str,
    camel_key: str,
) -> str:
    return str(
        event.get(snake_key)
        or event.get(camel_key)
        or payload.get(snake_key)
        or payload.get(camel_key)
        or ""
    ).strip()


def _suffix_prefix_overlap(left: str, right: str) -> int:
    max_len = min(len(left), len(right))
    for size in range(max_len, 0, -1):
        if left.endswith(right[:size]):
            return size
    return 0


__all__ = [
    "is_coalescible_stream_delta",
    "merge_stream_payload",
    "merge_stream_text",
    "stream_compaction_boundaries",
    "stream_events_can_coalesce",
    "stream_identity",
    "stream_text",
]
