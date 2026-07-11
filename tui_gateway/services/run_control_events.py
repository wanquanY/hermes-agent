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
    return event


def payload_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"failed", "error"}:
        return "error"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized == "interrupted":
        return "interrupted"
    return "complete"
