from __future__ import annotations

from cron import scheduler


class SessionCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def ensure(self, session_id: str, *, source: str, model: str | None) -> None:
        self.calls.append((session_id, source, model))


class MessageCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict]] = []

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        metadata: dict,
    ) -> None:
        self.calls.append((session_id, role, content, metadata))


class StateRoot:
    def __init__(self) -> None:
        self.sessions = SessionCommands()
        self.messages = MessageCommands()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_dovie_result_persists_through_session_and_message_services(monkeypatch):
    state = StateRoot()
    monkeypatch.setattr(scheduler, "open_cli_session_store", lambda: state)

    error = scheduler._append_dovie_session_message(
        {
            "id": "job-1",
            "name": "Daily report",
            "model": "test-model",
            "prompt": "Prepare report",
            "_execution_session_id": "execution-1",
        },
        target_session_id="stored-session",
        mode="current-session",
        success=True,
        final_response="Report ready",
        error=None,
    )

    assert error is None
    assert state.sessions.calls == [("stored-session", "tui", "test-model")]
    assert [(role, content) for _, role, content, _ in state.messages.calls] == [
        ("user", "Prepare report"),
        ("assistant", "Report ready"),
    ]
    assert state.messages.calls[1][3]["execution_session_id"] == "execution-1"
    assert state.closed is True
