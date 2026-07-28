"""Pure event identity helpers for run-control delivery."""

from __future__ import annotations

from typing import Any, Callable


_RUN_IDENTITY_EVENT_TYPE_PREFIXES = (
    "message.",
    "reasoning.",
    "thinking.",
    "tool.",
    "subagent.",
    "artifact.",
)
_RUN_IDENTITY_EVENT_TYPES = {"error"}


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


def _frame_requires_run_identity(event_type: str) -> bool:
    normalized = str(event_type or "").strip()
    if not normalized:
        return False
    if normalized in _RUN_IDENTITY_EVENT_TYPES:
        return True
    return normalized.startswith(_RUN_IDENTITY_EVENT_TYPE_PREFIXES)


def ensure_outbound_run_identity(
    params: dict[str, Any],
    *,
    on_missing_run: Callable[[dict[str, Any]], None],
    on_missing_turn: Callable[[dict[str, Any]], None],
) -> None:
    """Give run-scoped frames a stable identity before persistence/delivery."""
    if not isinstance(params, dict):
        return
    event_type = str(params.get("type") or "").strip()
    if not _frame_requires_run_identity(event_type):
        return
    run_id = event_run_id(params)
    turn_id = event_turn_id(params)
    if run_id and turn_id:
        return
    stable = conversation_session_id(params)
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else None
    if not run_id:
        run_id = f"synthetic-run:{stable or 'unknown-session'}:{turn_id or 'orphan'}"
        params["run_id"] = run_id
        params["synthetic_run_id"] = True
        if payload is not None:
            payload["run_id"] = run_id
        if not turn_id:
            turn_id = f"synthetic-turn:{run_id}"
            params["turn_id"] = turn_id
            if payload is not None:
                payload["turn_id"] = turn_id
        on_missing_run(
            {
                "event_type": event_type,
                "session_id": stable,
                "run_id": run_id,
                "turn_id": turn_id,
                "seq": params.get("seq"),
            }
        )
        return
    on_missing_turn(
        {
            "event_type": event_type,
            "session_id": stable,
            "run_id": run_id,
            "seq": params.get("seq"),
        }
    )


def capture_lifecycle_preludes(
    params: dict[str, Any],
    saved: Any,
) -> None:
    """Copy application-generated prelude events onto the outbound envelope."""
    if not isinstance(saved, dict):
        return
    events = saved.get("_lifecycle_prelude_events")
    if not isinstance(events, list):
        return
    params["_lifecycle_prelude_events"] = [
        dict(item) for item in events if isinstance(item, dict)
    ]


def take_lifecycle_preludes(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Remove internal prelude metadata before the parent frame is delivered."""
    value = params.pop("_lifecycle_prelude_events", [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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
    payload, text_stream = _stream_payload(event)
    subject = _stream_subject(event, payload, text_stream)
    event_type = str(
        payload.get("source_event_type")
        or payload.get("sourceEventType")
        or payload.get("event_type")
        or payload.get("eventType")
        or event.get("type")
        or ""
    ).strip()
    if not event_type.endswith((".delta", ".thinking")):
        return None
    return (
        event_type,
        str(
            text_stream.get("run_id")
            or text_stream.get("runId")
            or subject.get("run_id")
            or subject.get("runId")
            or payload.get("source_run_id")
            or payload.get("sourceRunId")
            or event_run_id(event)
        ).strip(),
        str(
            text_stream.get("turn_id")
            or text_stream.get("turnId")
            or subject.get("turn_id")
            or subject.get("turnId")
            or payload.get("source_turn_id")
            or payload.get("sourceTurnId")
            or event_turn_id(event)
        ).strip(),
        str(
            text_stream.get("runtime_scope_key")
            or text_stream.get("runtimeScopeKey")
            or subject.get("runtime_scope_key")
            or subject.get("runtimeScopeKey")
            or payload.get("source_runtime_scope_key")
            or payload.get("sourceRuntimeScopeKey")
            or event_runtime_scope_key(event)
        ).strip(),
        str(
            text_stream.get("subagent_id")
            or text_stream.get("subagentId")
            or text_stream.get("test_run_id")
            or text_stream.get("testRunId")
            or text_stream.get("stream_id")
            or text_stream.get("streamId")
            or text_stream.get("segment_id")
            or text_stream.get("segmentId")
            or text_stream.get("client_message_id")
            or text_stream.get("clientMessageId")
            or payload.get("subagent_id")
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


def _stream_subject(
    event: dict[str, Any],
    payload: dict[str, Any],
    text_stream: dict[str, Any],
) -> dict[str, Any]:
    """Return the source execution subject beneath a projected wrapper."""
    for candidate in (
        text_stream.get("subject"),
        event.get("subject"),
        payload.get("subject"),
    ):
        if isinstance(candidate, dict):
            return candidate
    return {}


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


def _project_stream_append(
    subscription: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any] | None:
    """Deliver only the unseen range of any append fragment.

    This is deliberately symmetric: live can arrive before durable, or a
    durable poll can win the race and be followed by a late live fan-out.
    Treating only durable checkpoints specially fixed one ordering while
    leaving the inverse ordering able to duplicate text.
    """
    identity = _stream_delivery_identity(event)
    if identity is None:
        return event
    payload, text_stream = _stream_payload(event)
    mode = str(text_stream.get("mode") or payload.get("mode") or "append").strip().lower()
    if mode != "append":
        # Snapshot/replace events intentionally rewrite an earlier prefix and
        # must remain authoritative.
        return event
    raw_offset = text_stream.get("offset", payload.get("offset"))
    if raw_offset is None:
        # The runtime stream registry stamps offsets on new frames. Keeping
        # this compatibility path avoids guessing for older persisted rows.
        return event
    offsets = subscription.get("direct_stream_offsets")
    delivered_end = int(offsets.get(identity) or 0) if isinstance(offsets, dict) else 0
    try:
        fragment_offset = max(0, int(raw_offset or 0))
    except (TypeError, ValueError):
        return event
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
    fragment_end = fragment_offset + _utf16_length(text)
    if delivered_end <= fragment_offset:
        return event
    if delivered_end >= fragment_end:
        return None

    # The subscriber saw only a prefix. Deliver exactly the unseen suffix while
    # retaining this event's canonical cursor/causal identity.
    projected = dict(event)
    projected_payload = dict(payload)
    suffix = _slice_utf16(text, delivered_end - fragment_offset)
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
    return _project_stream_append(subscription, event)


def payload_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"failed", "error"}:
        return "error"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized == "interrupted":
        return "interrupted"
    return "complete"
