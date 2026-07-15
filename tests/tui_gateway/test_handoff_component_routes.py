from __future__ import annotations

import importlib
from contextlib import contextmanager
from types import SimpleNamespace

from hermes_gateway.config import Platform
from tui_gateway import server


handoff_methods = importlib.import_module("tui_gateway.methods.handoff")


class SessionHandoffQueries:
    def __init__(self) -> None:
        self.rows = {"session-1": {"id": "session-1"}}
        self.state = {"state": "pending", "platform": "telegram", "error": ""}
        self.requested: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    def get(self, session_id: str):
        return self.rows.get(session_id)

    def set_title(self, session_id: str, title: str) -> bool:
        self.rows[session_id] = {"id": session_id, "title": title}
        return True

    def request_handoff(self, session_id: str, platform: str) -> bool:
        self.requested.append((session_id, platform))
        return True

    def handoff_state(self, session_id: str):
        assert session_id == "session-1"
        return dict(self.state)

    def fail_handoff(self, session_id: str, error: str) -> None:
        self.failed.append((session_id, error))
        self.state = {"state": "failed", "platform": "telegram", "error": error}


def _install_handoff_context(monkeypatch, sessions: SessionHandoffQueries) -> None:
    state = SimpleNamespace(sessions=sessions)

    @contextmanager
    def session_db(_session):
        yield state

    monkeypatch.setattr(
        handoff_methods,
        "_sess_nowait",
        lambda _params, _rid: ({"session_key": "session-1", "running": False}, None),
    )
    monkeypatch.setattr(handoff_methods, "_session_db", session_db)
    monkeypatch.setattr(handoff_methods, "_ensure_session_db_row", lambda _session: None)


def test_handoff_request_uses_session_component(monkeypatch):
    sessions = SessionHandoffQueries()
    _install_handoff_context(monkeypatch, sessions)
    monkeypatch.setattr(
        "hermes_gateway.config.load_gateway_config",
        lambda: SimpleNamespace(
            platforms={Platform.TELEGRAM: SimpleNamespace(enabled=True)},
            get_home_channel=lambda _platform: SimpleNamespace(
                chat_id="chat-1",
                name="Telegram Home",
            ),
        ),
    )

    response = server.handle_request(
        {
            "id": "handoff-request",
            "method": "handoff.request",
            "params": {"platform": "telegram"},
        }
    )

    assert "error" not in response, response
    assert response["result"]["queued"] is True
    assert sessions.requested == [("session-1", "telegram")]


def test_handoff_state_uses_session_component(monkeypatch):
    sessions = SessionHandoffQueries()
    _install_handoff_context(monkeypatch, sessions)

    response = server.handle_request(
        {"id": "handoff-state", "method": "handoff.state", "params": {}}
    )

    assert response["result"] == {
        "state": "pending",
        "platform": "telegram",
        "error": "",
    }


def test_handoff_fail_uses_session_component(monkeypatch):
    sessions = SessionHandoffQueries()
    _install_handoff_context(monkeypatch, sessions)

    response = server.handle_request(
        {
            "id": "handoff-fail",
            "method": "handoff.fail",
            "params": {"error": "poll timed out"},
        }
    )

    assert response["result"] == {"failed": True, "state": "failed"}
    assert sessions.failed == [("session-1", "poll timed out")]
