"""TUI close/resume lifecycle ownership contracts."""

from __future__ import annotations

import threading

from tui_gateway import server


def test_pop_session_resolves_conversation_id_atomically():
    runtime_sid = "runtime-pop-test"
    session = {"session_key": "conversation-pop-test"}
    server._sessions[runtime_sid] = session
    try:
        claimed = server._pop_session_by_id("conversation-pop-test")
        assert claimed == (runtime_sid, session)
        assert runtime_sid not in server._sessions
        assert session["_sid"] == runtime_sid
    finally:
        server._sessions.pop(runtime_sid, None)


def test_session_close_releases_resume_lock_before_slow_teardown(monkeypatch):
    """Slow finalization cannot block unrelated resume ownership checks."""
    runtime_sid = "runtime-slow-close"
    conversation_id = "conversation-slow-close"
    session = {"session_key": conversation_id}
    server._sessions[runtime_sid] = session
    teardown_started = threading.Event()
    release_teardown = threading.Event()
    response: dict = {}

    def slow_teardown(claimed, *, end_reason="tui_close"):
        assert claimed == (runtime_sid, session)
        assert end_reason == "tui_close"
        teardown_started.set()
        assert release_teardown.wait(timeout=3.0)
        return True

    monkeypatch.setattr(server, "_teardown_popped_session", slow_teardown)

    def close_session():
        response.update(
            server.handle_request(
                {
                    "id": "close",
                    "method": "session.close",
                    "params": {"session_id": conversation_id},
                }
            )
        )

    thread = threading.Thread(target=close_session)
    thread.start()
    acquired = False
    try:
        assert teardown_started.wait(timeout=2.0)
        assert runtime_sid not in server._sessions
        acquired = server._session_resume_lock.acquire(timeout=0.25)
        assert acquired, "slow teardown retained the global resume lock"
    finally:
        if acquired:
            server._session_resume_lock.release()
        release_teardown.set()
        thread.join(timeout=3.0)
        server._sessions.pop(runtime_sid, None)

    assert not thread.is_alive()
    assert response["result"] == {
        "closed": True,
        "session_id": runtime_sid,
        "conversation_session_id": conversation_id,
    }
