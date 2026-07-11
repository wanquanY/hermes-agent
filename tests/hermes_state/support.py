from __future__ import annotations


def business_payload(
    event: dict,
    *,
    session_id: str = "stored-1",
    execution_session_id: str = "runtime-1",
) -> dict:
    payload = dict(event.get("payload") or {})
    assert payload.pop("session_id") == session_id
    assert payload.pop("conversation_session_id") == session_id
    assert payload.pop("execution_session_id") == execution_session_id
    return payload
