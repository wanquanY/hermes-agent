"""Unit tests for ``tui_gateway.services.agent_runner`` — Phase 5c.2.

Most of the runner's logic is integration-shaped (touches the real
``tui_gateway.server`` module + agent build), so these tests focus on
the parts that ARE unit-testable in isolation:

- ``_NoopTransport.write`` returns True (write_json / _emit treat that
  as "delivered")
- ``setup_worker_environment`` is idempotent across calls
- ``setup_worker_environment`` replaces ``server._stdio_transport``
  with a ``_NoopTransport`` so any stray legacy write_json becomes
  a silent no-op instead of leaking a JSON-RPC envelope onto the
  protocol pipe

End-to-end behavior (RunStartFrame → message.complete EventFrame
arriving on the supervisor) is dev-app validated, not unit tested —
the prompt path drags in the full LLM stack."""

from __future__ import annotations

import threading

import pytest

from tui_gateway.run_worker import RunStartFrame
from tui_gateway.services.agent_runner import (
    _NoopTransport,
    _ensure_worker_session,
    setup_worker_environment,
)


def test_noop_transport_write_returns_true() -> None:
    t = _NoopTransport()
    assert t.write({"jsonrpc": "2.0", "method": "event", "params": {}}) is True
    assert t.is_connected() is True


def test_setup_worker_environment_neutralizes_stdio_transport() -> None:
    setup_worker_environment()
    from tui_gateway import server as _server
    assert isinstance(_server._stdio_transport, _NoopTransport)
    # Calling write_json with anything must succeed against the noop.
    assert _server.write_json({"jsonrpc": "2.0", "method": "event", "params": {}}) is True


def test_setup_worker_environment_is_idempotent() -> None:
    setup_worker_environment()
    from tui_gateway import server as _server
    first = _server._stdio_transport
    setup_worker_environment()
    second = _server._stdio_transport
    # Same instance — second call must NOT replace it again.
    assert first is second


def test_run_start_frame_carries_all_fields_for_runner() -> None:
    """Smoke test that the protocol frame contract has every field the
    runner reads. Catches accidental removals in the protocol."""
    frame = RunStartFrame(
        run_id="r1",
        turn_id="t1",
        stored_session_id="20260625_120000_abcdef",
        prompt="hello",
        params={"runtime_scope_key": "profile:test", "cwd": "/tmp"},
    )
    assert frame.stored_session_id
    assert frame.run_id
    assert frame.turn_id
    assert isinstance(frame.params, dict)


def test_worker_session_defers_agent_build_until_prompt_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker bootstrap must not race prompt-level toolset overrides."""
    from tui_gateway import server as _server

    starts: list[tuple[str, dict]] = []
    sessions: dict[str, dict] = {}
    monkeypatch.setattr(_server, "_sessions", sessions)
    monkeypatch.setattr(_server, "_sessions_lock", threading.Lock())
    monkeypatch.setattr(_server, "_stdio_transport", _NoopTransport())
    monkeypatch.setattr(_server, "_db_for_stable_session", lambda _sid: None)
    monkeypatch.setattr(
        _server,
        "_start_agent_build",
        lambda sid, session: starts.append((sid, session)),
    )

    sid, session = _ensure_worker_session(
        RunStartFrame(
            run_id="team-run-1",
            turn_id="team-turn-1",
            stored_session_id="team-session-team-conversation-1",
            prompt="start team task",
            params={
                "runtime_scope_key": "team:team-conversation-1:leader-conversation",
                "enabled_toolsets": ["team_mission_conversation_leader"],
                "disabled_toolsets": ["delegation"],
                "toolset_scope": "exact",
            },
        )
    )

    assert sessions[sid] is session
    assert session["agent"] is None
    assert starts == []


def test_worker_session_restores_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from tui_gateway import server as _server
    from tui_gateway.services import agent_runner

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    sessions: dict[str, dict] = {}
    monkeypatch.setattr(_server, "_sessions", sessions)
    monkeypatch.setattr(_server, "_sessions_lock", threading.Lock())
    monkeypatch.setattr(_server, "_stdio_transport", _NoopTransport())
    monkeypatch.setattr(_server, "_db_for_stable_session", lambda _sid: None)
    monkeypatch.setattr(
        agent_runner,
        "session_workspace_run_context",
        lambda _session_id, _params: {
            "cwd": str(workspace_root),
            "workspace": {
                "id": "workspace-1",
                "name": "Workspace One",
                "path": str(workspace_root),
                "kind": "local",
            },
            "source": "session_workspace_binding",
        },
    )

    sid, session = _ensure_worker_session(
        RunStartFrame(
            run_id="run-1",
            turn_id="turn-1",
            stored_session_id="stored-session-1",
            prompt="pwd",
            params={"runtime_scope_key": "profile:test"},
        )
    )

    assert sessions[sid] is session
    assert session["cwd"] == str(workspace_root)
    assert session["workspace"]["id"] == "workspace-1"
