"""Tests for TUI /undo command.dispatch handling."""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        if "tui_gateway.server" in sys.modules:
            mod = importlib.reload(sys.modules["tui_gateway.server"])
        else:
            mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()
        mod._methods.clear()
        importlib.reload(mod)


@pytest.fixture()
def db(hermes_home):
    session_db = open_cli_session_store(db_path=hermes_home / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def session_with_history(server, db):
    sid = "sid-undo"
    session_key = "tui-undo-1"
    db.sessions.create(session_key, source="tui")
    for i in range(1, 4):
        db.messages.append(session_key, "user", f"question {i}")
        db.messages.append(session_key, "assistant", f"answer {i}")
    history = db.messages.all_as_conversation(session_key)
    agent = MagicMock()
    agent._memory_manager = MagicMock()
    agent._last_flushed_db_idx = len(history)
    session = {
        "session_key": session_key,
        "history": list(history),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "agent": agent,
    }
    server._sessions[sid] = session
    server._db = db
    return sid, session_key, session, agent


def _call(server, method, **params):
    return server._methods[method](1, params)


def test_undo_returns_prefill_with_target_text(server, session_with_history):
    sid, _, _, _ = session_with_history

    response = _call(server, "command.dispatch", session_id=sid, name="undo", arg="")

    assert "error" not in response
    assert response["result"]["type"] == "prefill"
    assert response["result"]["message"] == "question 3"
    assert "Undid" in response["result"]["notice"]


def test_undo_truncates_in_memory_and_db_history(server, session_with_history, db):
    sid, session_key, session, _ = session_with_history

    _call(server, "command.dispatch", session_id=sid, name="undo", arg="")

    assert [message["content"] for message in session["history"]] == [
        "question 1",
        "answer 1",
        "question 2",
        "answer 2",
    ]
    assert session["history_version"] == 1
    all_rows = db.messages.list(session_key, include_inactive=True)
    assert [row["active"] for row in all_rows] == [1, 1, 1, 1, 0, 0]
    assert db.sessions.get(session_key)["rewind_count"] == 1


def test_undo_n_backs_up_multiple_user_turns(server, session_with_history):
    sid, _, session, _ = session_with_history

    response = _call(server, "command.dispatch", session_id=sid, name="undo", arg="2")

    assert response["result"]["message"] == "question 2"
    assert "2 turns" in response["result"]["notice"]
    assert [message["content"] for message in session["history"]] == [
        "question 1",
        "answer 1",
    ]


def test_undo_notifies_memory_provider(server, session_with_history):
    sid, session_key, _, agent = session_with_history

    _call(server, "command.dispatch", session_id=sid, name="undo", arg="")

    agent._memory_manager.on_session_switch.assert_called_once()
    args, kwargs = agent._memory_manager.on_session_switch.call_args
    assert args[0] == session_key
    assert kwargs["rewound"] is True
    assert kwargs["reset"] is False


def test_undo_rejects_invalid_count(server, session_with_history):
    sid, _, _, _ = session_with_history

    response = _call(server, "command.dispatch", session_id=sid, name="undo", arg="abc")

    assert "error" in response
    assert response["error"]["code"] == 4004
    assert "invalid count" in response["error"]["message"]


def test_undo_refuses_when_session_busy(server, session_with_history):
    sid, _, session, _ = session_with_history
    session["running"] = True

    response = _call(server, "command.dispatch", session_id=sid, name="undo", arg="")

    assert "error" in response
    assert "busy" in response["error"]["message"].lower()


def test_rewind_alias_uses_same_prefill_contract(server, session_with_history):
    sid, _, _, _ = session_with_history

    response = _call(server, "command.dispatch", session_id=sid, name="rewind", arg="")

    assert response["result"]["type"] == "prefill"
    assert response["result"]["message"] == "question 3"
    assert "Rewound" in response["result"]["notice"]


def test_undo_in_pending_input_commands(server):
    assert "undo" in server._PENDING_INPUT_COMMANDS
    assert "rewind" in server._PENDING_INPUT_COMMANDS
