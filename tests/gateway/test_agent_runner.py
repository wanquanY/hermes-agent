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
from hermes_team_mission.domain.run_context import RunContext


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
        conversation_session_id="20260625_120000_abcdef",
        prompt="hello",
        params={"runtime_scope_key": "profile:test", "cwd": "/tmp"},
    )
    assert frame.conversation_session_id
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
            conversation_session_id="team-session-team-conversation-1",
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


def test_team_leader_worker_hydrates_member_replies_as_observed_group_speech(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from tui_gateway import server as _server

    from hermes_agent.storage.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(
        session_id="team-session-team-conversation-1",
        source="team",
        conversation_kind="team",
    )
    for index, message in enumerate(
        [
            ("user", "你是谁？", "user"),
            ("assistant", "我是小多，负责团队协调。", "leader:team-conversation-1"),
            ("assistant", "我是前端工程师，负责 UI。", "member:frontend"),
        ],
        start=1,
    ):
        db.messages.append(
            "team-session-team-conversation-1",
            role=message[0],
            content=message[1],
            participant_id=message[2],
            timestamp=float(index),
        )
    monkeypatch.setattr(
        db.participants,
        "list_conversation_participants",
        lambda session_id: [
            {"participant_id": "leader:team-conversation-1", "display_name": "小多"},
            {"participant_id": "member:frontend", "display_name": "前端工程师"},
        ],
    )

    control_home = str(tmp_path / "control")
    execution_home = str(tmp_path / "execution")
    monkeypatch.setattr(_server, "_sessions", {})
    monkeypatch.setattr(_server, "_sessions_lock", threading.Lock())
    monkeypatch.setattr(_server, "_stdio_transport", _NoopTransport())
    monkeypatch.setattr(_server, "_db_for_stable_session", lambda _sid: db)

    _, session = _ensure_worker_session(
        RunStartFrame(
            run_id="team-leader-run-1",
            turn_id="team-leader-turn-1",
            conversation_session_id="team-session-team-conversation-1",
            prompt="总结一下我们的对话记录",
            params={
                "cwd": str(tmp_path),
                "runtime_scope_key": "team:team-conversation-1:leader-conversation",
                "run_context_json": RunContext(
                    conversation_session_id="team-session-team-conversation-1",
                    participant_id="leader:team-conversation-1",
                    activity_id="chat:team-conversation-1",
                    activity_kind="chat",
                    execution_scope_key="team:team-conversation-1:leader-conversation",
                    control_home=control_home,
                    execution_home=execution_home,
                ).to_payload(),
            },
        )
    )

    assert [msg["role"] for msg in session["history"]] == [
        "user", "assistant", "user"
    ]
    assert session["history"][0]["content"] == "你是谁？"
    assert session["history"][1]["content"] == "我是小多，负责团队协调。"
    assert "name" not in session["history"][1]
    assert session["history"][2]["content"] == (
        "[assistant | 前端工程师 | member:frontend]\n我是前端工程师，负责 UI。"
    )
    assert "name" not in session["history"][2]
    assert session["history"][2]["metadata"]["speaker_participant_id"] == "member:frontend"
    assert session["history"][2]["metadata"]["speaker_projected_role"] == "user"


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
            conversation_session_id="stored-session-1",
            prompt="pwd",
            params={"runtime_scope_key": "profile:test"},
        )
    )

    assert sessions[sid] is session
    assert session["cwd"] == str(workspace_root)
    assert session["workspace"]["id"] == "workspace-1"
