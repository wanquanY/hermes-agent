"""Pure event identity helpers for run-control delivery."""

from __future__ import annotations

from typing import Any


def conversation_session_id(params: dict[str, Any]) -> str:
    raw_payload = params.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else None
    payload_lookup = payload or {}
    return str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or payload_lookup.get("conversation_session_id")
        or payload_lookup.get("conversationSessionId")
        or payload_lookup.get("session_key")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()


def stamp_session_identity(
    params: dict[str, Any],
    *,
    conversation_id: str = "",
    execution_session_id: str = "",
) -> dict[str, Any]:
    """Stamp the two canonical event identities on an outbound frame.

    ``conversation_session_id`` routes the durable visible timeline and
    ``execution_session_id`` identifies the worker execution container. The
    canonical ``session_id`` is the conversation id; legacy wire aliases are
    folded exclusively by :mod:`hermes_agent.gateway.pipeline`.
    """
    if not isinstance(params, dict):
        return params
    raw_payload = params.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else None
    payload_lookup = payload or {}
    conversation_id = str(conversation_id or conversation_session_id(params) or "").strip()
    execution_id = str(
        execution_session_id
        or params.get("execution_session_id")
        or params.get("executionSessionId")
        or payload_lookup.get("execution_session_id")
        or payload_lookup.get("executionSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    if conversation_id:
        params["session_id"] = conversation_id
        params["conversation_session_id"] = conversation_id
        if payload is not None:
            payload["session_id"] = conversation_id
            payload["conversation_session_id"] = conversation_id
    if execution_id:
        params["execution_session_id"] = execution_id
        if payload is not None:
            payload["execution_session_id"] = execution_id
    return params


def event_run_id(params: dict[str, Any]) -> str:
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    return str(params.get("run_id") or payload.get("run_id") or "").strip()


def event_turn_id(params: dict[str, Any]) -> str:
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    return str(params.get("turn_id") or payload.get("turn_id") or "").strip()


def event_runtime_scope_key(params: dict[str, Any]) -> str:
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    return str(
        params.get("runtime_scope_key")
        or payload.get("runtime_scope_key")
        or ""
    ).strip()


def stream_text_delta(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for key in ("delta", "text", "output"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def terminal_delivery_identity(event: dict[str, Any]) -> tuple[Any, ...] | None:
    event_type = str(event.get("type") or "").strip()
    if event_type not in {"message.complete", "error", "session.interrupted", "session.recalled"}:
        return None
    return (
        event_type,
        event_run_id(event),
        event_turn_id(event),
        event_runtime_scope_key(event),
    )


def remember_terminal_delivery(subscription: dict[str, Any], event: dict[str, Any]) -> None:
    identity = terminal_delivery_identity(event)
    if not identity:
        return
    identities = subscription.get("direct_terminal_identities")
    if not isinstance(identities, set):
        identities = set(identities or ())
        subscription["direct_terminal_identities"] = identities
    identities.add(identity)
    if len(identities) > 2000:
        for value in list(identities)[: len(identities) - 2000]:
            identities.discard(value)


def _stream_delivery_identity(event: dict[str, Any]) -> tuple[Any, ...] | None:
    event_type = str(event.get("type") or "").strip()
    if not event_type.endswith((".delta", ".thinking")):
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return (
        event_type,
        event_run_id(event),
        event_turn_id(event),
        event_runtime_scope_key(event),
        str(
            payload.get("subagent_id")
            or payload.get("subagentId")
            or payload.get("test_run_id")
            or payload.get("testRunId")
            or payload.get("stream_id")
            or payload.get("streamId")
            or payload.get("segment_id")
            or payload.get("segmentId")
            or payload.get("client_message_id")
            or payload.get("clientMessageId")
            or event.get("activity_id")
            or "default"
        ).strip(),
    )


def _utf16_length(value: str) -> int:
    return len(str(value or "").encode("utf-16-le")) // 2


def _slice_utf16(value: str, start: int) -> str:
    raw = str(value or "").encode("utf-16-le")
    return raw[max(0, start) * 2 :].decode("utf-16-le", errors="ignore")


def _stream_payload(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text_stream = (
        event.get("text_stream")
        if isinstance(event.get("text_stream"), dict)
        else payload.get("text_stream")
        if isinstance(payload.get("text_stream"), dict)
        else {}
    )
    return payload, text_stream


def remember_stream_delivery(subscription: dict[str, Any], event: dict[str, Any]) -> None:
    """Track live stream coverage independently from durable event sequence.

    Token deltas are intentionally transient and therefore have no canonical
    ``seq``.  A structural boundary later persists one coalesced checkpoint.
    Without a second cursor, the subscription poller can race that boundary and
    redeliver the already-rendered prefix before the following structural event
    advances ``last_seq``.
    """
    identity = _stream_delivery_identity(event)
    if identity is None:
        return
    payload, text_stream = _stream_payload(event)
    text = next(
        (
            value
            for value in (
                text_stream.get("delta"),
                text_stream.get("text"),
                payload.get("delta"),
                payload.get("text"),
                payload.get("output"),
            )
            if isinstance(value, str)
        ),
        "",
    )
    if not text:
        return
    offsets = subscription.get("direct_stream_offsets")
    if not isinstance(offsets, dict):
        offsets = {}
        subscription["direct_stream_offsets"] = offsets
    previous = max(0, int(offsets.get(identity) or 0))
    mode = str(text_stream.get("mode") or payload.get("mode") or "append").strip().lower()
    raw_offset = text_stream.get("offset", payload.get("offset"))
    try:
        offset = max(0, int(raw_offset)) if raw_offset is not None else previous
    except (TypeError, ValueError):
        offset = previous
    end_offset = _utf16_length(text) if mode in {"snapshot", "replace", "cumulative"} else offset + _utf16_length(text)
    offsets[identity] = max(previous, end_offset)


def _project_stream_checkpoint(
    subscription: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any] | None:
    """Suppress/crop a durable append checkpoint already seen live."""
    identity = _stream_delivery_identity(event)
    if identity is None:
        return event
    payload, text_stream = _stream_payload(event)
    if not bool(payload.get("stream_checkpoint") or payload.get("streamCheckpoint")):
        return event
    mode = str(text_stream.get("mode") or payload.get("mode") or "append").strip().lower()
    if mode != "append":
        # Snapshot checkpoints may intentionally rewrite an earlier prefix and
        # must remain authoritative.
        return event
    offsets = subscription.get("direct_stream_offsets")
    delivered_end = int(offsets.get(identity) or 0) if isinstance(offsets, dict) else 0
    try:
        checkpoint_offset = max(
            0,
            int(text_stream.get("offset", payload.get("offset", 0)) or 0),
        )
    except (TypeError, ValueError):
        checkpoint_offset = 0
    text = next(
        (
            value
            for value in (
                text_stream.get("delta"),
                text_stream.get("text"),
                payload.get("delta"),
                payload.get("text"),
                payload.get("output"),
            )
            if isinstance(value, str)
        ),
        "",
    )
    checkpoint_end = checkpoint_offset + _utf16_length(text)
    if delivered_end <= checkpoint_offset:
        return event
    if delivered_end >= checkpoint_end:
        return None

    # The live transport saw only a prefix. Deliver exactly the unseen suffix
    # while retaining the checkpoint's canonical seq/identity.
    projected = dict(event)
    projected_payload = dict(payload)
    suffix = _slice_utf16(text, delivered_end - checkpoint_offset)
    projected_payload.update({"offset": delivered_end, "text": suffix, "delta": suffix})
    if "output" in projected_payload:
        projected_payload["output"] = suffix
    projected_text_stream = dict(text_stream)
    projected_text_stream.update({"offset": delivered_end, "text": suffix, "delta": suffix})
    projected_payload["text_stream"] = projected_text_stream
    projected["payload"] = projected_payload
    projected["text_stream"] = dict(projected_text_stream)
    return projected


def was_terminal_delivered(subscription: dict[str, Any], event: dict[str, Any]) -> bool:
    identity = terminal_delivery_identity(event)
    if not identity:
        return False
    identities = subscription.get("direct_terminal_identities")
    return isinstance(identities, set) and identity in identities


def delta_event_for_subscription(
    subscription: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any] | None:
    if was_terminal_delivered(subscription, event):
        return None
    return _project_stream_checkpoint(subscription, event)


def payload_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"failed", "error"}:
        return "error"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized == "interrupted":
        return "interrupted"
    return "complete"
