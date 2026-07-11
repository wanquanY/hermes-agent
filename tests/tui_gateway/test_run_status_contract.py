"""Contract tests for the run.status result shape."""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()
        mod._methods.clear()
        importlib.reload(mod)


def test_run_status_returns_common_top_level_fields_for_run_and_session_paths(server, monkeypatch):
    class _RunDB:
        def __init__(self):
            self.runs = self
            self.run = {
                "run_id": "run-common",
                "turn_id": "turn-common",
                "session_id": "stored-common",
                "status": "running",
                "runtime_scope_key": "profile:agent-a",
                "started_at": 11.0,
                "updated_at": 22.0,
                "last_seq": 9,
            }

        def get(self, run_id):
            return dict(self.run) if run_id == "run-common" else None

        def list(
            self,
            session_id,
            runtime_scope_key="",
            statuses=None,
            limit=200,
        ):
            return [dict(self.run)] if session_id == "stored-common" else []

        def session_status(self, session_id):
            assert session_id == "stored-common"
            return {
                "running": True,
                "active_run_id": "run-common",
                "active_turn_id": "turn-common",
                "runtime_scope_key": "profile:agent-a",
                "run_started_at": 11.0,
                "run_updated_at": 22.0,
                "last_event_seq": 9,
            }

    db = _RunDB()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_db_for_stable_session", lambda _stable: db)

    by_run = server.handle_request(
        {
            "id": "by-run",
            "method": "run.status",
            "params": {"run_id": "run-common"},
        }
    )
    by_session = server.handle_request(
        {
            "id": "by-session",
            "method": "run.status",
            "params": {"conversation_session_id": "stored-common"},
        }
    )

    assert "error" not in by_run
    assert "error" not in by_session
    expected_common = {
        "status": "running",
        "run_id": "run-common",
        "conversation_session_id": "stored-common",
        "last_event_seq": 9,
    }
    assert {key: by_run["result"][key] for key in expected_common} == expected_common
    assert {key: by_session["result"][key] for key in expected_common} == expected_common
    assert by_run["result"]["run"]["status"] == "running"
    assert by_session["result"]["active_run_id"] == "run-common"
    assert "run" not in by_session["result"]
