from __future__ import annotations

from tui_gateway.services import run_control


class _ScopedDB:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path


def test_session_replay_reads_only_the_requested_db_memory_lane() -> None:
    session_id = "shared-conversation-1"
    db_a = _ScopedDB("/profiles/a/state.db")
    db_b = _ScopedDB("/profiles/b/state.db")
    event_a = {
        "type": "message.delta",
        "session_id": session_id,
        "conversation_session_id": session_id,
        "run_id": "run-a",
        "seq": 1,
        "payload": {"delta": "profile-a"},
    }
    event_b = {
        "type": "message.delta",
        "session_id": session_id,
        "conversation_session_id": session_id,
        "run_id": "run-b",
        "seq": 2,
        "payload": {"delta": "profile-b"},
    }

    with run_control._lock:
        run_control._events_by_session.clear()
        run_control._events_by_session[
            run_control._memory_session_key(session_id, db_a)
        ].append(event_a)
        run_control._events_by_session[
            run_control._memory_session_key(session_id, db_b)
        ].append(event_b)

    try:
        _subscription_id, events = run_control.subscribe_session_with_id(
            conversation_session_id=session_id,
            transport=None,
            db=db_a,
        )
        assert events == [event_a]
    finally:
        with run_control._lock:
            run_control._events_by_session.clear()
