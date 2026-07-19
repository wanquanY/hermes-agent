"""Tests for /goal handling in tui_gateway.

The TUI routes ``/goal`` through ``command.dispatch`` (not ``slash.exec``)
because the CLI's ``_handle_goal_command`` queues the kickoff message onto
``_pending_input``, which the slash-worker subprocess has no reader for.
Instead we handle ``/goal`` directly in the server and return a
``{"type": "send", "notice": ..., "message": ...}`` payload the TUI client
uses to render a system line and fire the kickoff prompt.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Bust the goal-module store cache so it re-resolves HERMES_HOME.
    from hermes_cli import goals

    goals._STORE_CACHE.clear()
    yield home
    goals._STORE_CACHE.clear()


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        if "goal.set" not in mod._methods or "command.dispatch" not in mod._methods:
            mod._register_extracted_method_modules()
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def session(server):
    sid = "sid-test"
    session_key = "tui-goal-session-1"
    s = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = s
    return sid, session_key, s


def _call(server, method, **params):
    handler = server._methods[method]
    return handler(1, params)


# ── command.dispatch /goal ────────────────────────────────────────────


def test_goal_bare_shows_status_when_none_set(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_whitespace_only_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="   ", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_status_alias_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="status", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_set_returns_send_with_notice(server, session):
    sid, session_key, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="build a rocket", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "build a rocket"
    assert "notice" in result
    assert "Goal set" in result["notice"]
    assert "20-turn budget" in result["notice"]

    # Persisted in shared session state.
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_key)
    assert mgr.state is not None
    assert mgr.state.goal == "build a rocket"
    assert mgr.state.status == "active"


def test_goal_set_parses_completion_contract(server, session):
    sid, session_key, _ = session
    r = _call(
        server,
        "command.dispatch",
        name="goal",
        arg="Ship it\nverify: pytest passes\nconstraints: preserve the API",
        session_id=sid,
    )
    assert "Completion contract" in r["result"]["notice"]

    from hermes_cli.goals import GoalManager

    state = GoalManager(session_key).state
    assert state.goal == "Ship it"
    assert state.contract.verification == "pytest passes"
    assert state.contract.constraints == "preserve the API"


def test_goal_show_returns_contract(server, session):
    sid, _, _ = session
    _call(
        server,
        "command.dispatch",
        name="goal",
        arg="Ship it\nverify: pytest passes",
        session_id=sid,
    )
    r = _call(server, "command.dispatch", name="goal", arg="show", session_id=sid)
    assert "Verification: pytest passes" in r["result"]["output"]


def test_goal_pause_after_set(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "paused" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.status == "paused"


def test_goal_resume_reactivates(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="resume", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "resumed" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.status == "active"


def test_goal_clear_removes_active_goal(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="clear", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "cleared" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    # After clear the row is marked status=cleared (kept for audit);
    # ``has_goal()`` / ``is_active()`` return False so the goal loop
    # stays off and ``status`` reports "No active goal".
    mgr = GoalManager(session_key)
    assert not mgr.has_goal()
    assert not mgr.is_active()
    assert "No active goal" in mgr.status_line()


def test_goal_stop_and_done_are_clear_aliases(server, session):
    sid, _, _ = session
    _call(server, "command.dispatch", name="goal", arg="first goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="stop", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()

    _call(server, "command.dispatch", name="goal", arg="second goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="done", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()


def test_goal_requires_session(server):
    r = _call(server, "command.dispatch", name="goal", arg="nope", session_id="unknown")
    assert "error" in r
    assert r["error"]["code"] == 4001


# ── first-class goal RPC used by desktop clients ──────────────────────


def test_goal_set_rpc_resolves_stable_conversation_identity(server, session):
    _sid, session_key, _ = session
    r = _call(
        server,
        "goal.set",
        conversation_session_id=session_key,
        goal="Refactor auth\nverify: pytest tests/auth passes",
    )
    assert r["result"]["active"] is True
    assert r["result"]["status"] == "active"
    assert r["result"]["goal"] == "Refactor auth"
    assert r["result"]["kickoff_message"] == "Refactor auth"
    assert r["result"]["contract"]["verification"] == "pytest tests/auth passes"

    status = _call(server, "goal.get", stored_session_id=session_key)
    assert status["result"]["active"] is True
    assert status["result"]["goal"] == "Refactor auth"


def test_goal_set_rpc_rejects_running_session(server, session):
    _sid, session_key, value = session
    value["running"] = True
    r = _call(server, "goal.set", session_id=session_key, goal="race the active run")
    assert r["error"]["code"] == 4009


def test_goal_control_rpcs_share_one_persisted_state(server, session):
    _sid, session_key, _ = session
    _call(server, "goal.set", session_id=session_key, goal="Ship it")
    assert _call(server, "goal.pause", session_id=session_key)["result"]["status"] == "paused"
    assert _call(server, "goal.resume", session_id=session_key)["result"]["status"] == "active"
    cleared = _call(server, "goal.clear", session_id=session_key)
    assert cleared["result"]["active"] is False
    assert cleared["result"]["status"] == "none"


def test_goal_resume_rpc_honors_string_false_reset_budget(server, session):
    _sid, session_key, _ = session
    _call(server, "goal.set", session_id=session_key, goal="Ship it")
    from hermes_cli.goals import GoalManager

    manager = GoalManager(session_id=session_key)
    manager.state.turns_used = 4
    manager.pause()
    resumed = _call(
        server,
        "goal.resume",
        session_id=session_key,
        reset_budget="false",
    )
    assert resumed["result"]["turns_used"] == 4


# ── slash.exec /goal routing ──────────────────────────────────────────


def test_slash_exec_rejects_goal_routes_to_command_dispatch(server, session):
    """slash.exec must reject /goal with 4018 so the TUI client falls through
    to command.dispatch. Without this, the HermesCLI slash-worker subprocess
    would set the goal but silently drop the kickoff — the queue is in-proc."""
    sid, _, _ = session
    r = _call(server, "slash.exec", command="goal status", session_id=sid)
    assert "error" in r
    assert r["error"]["code"] == 4018
    assert "command.dispatch" in r["error"]["message"]


def test_pending_input_commands_includes_goal(server):
    """Guard: _PENDING_INPUT_COMMANDS must list 'goal' — removing it would
    silently re-break the TUI."""
    assert "goal" in server._PENDING_INPUT_COMMANDS
