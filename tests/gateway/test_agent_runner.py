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
    _clear_runtime_capabilities,
    _ensure_worker_session,
    _import_runtime_capabilities,
    _watch_for_cancel,
    setup_worker_environment,
)
from hermes_team_mission.domain.run_context import RunContext


@pytest.fixture(autouse=True)
def _restore_worker_environment_globals():
    """Keep process-global worker bootstrap state isolated between tests."""
    from tui_gateway import server as _server
    from tui_gateway.services import agent_runner as _agent_runner

    original_setup_done = _agent_runner._setup_done
    original_stdio_transport = _server._stdio_transport
    try:
        yield
    finally:
        _agent_runner._setup_done = original_setup_done
        _server._stdio_transport = original_stdio_transport


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


def test_worker_cancel_cascades_to_owned_detached_subagents_before_parent_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_agent.application.subagent_execution_service import (
        subagent_execution_runtime,
    )
    from tui_gateway.methods import session as session_methods

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        subagent_execution_runtime,
        "cancel_owner_run",
        lambda **kwargs: calls.append(("children", kwargs)) or 3,
    )
    monkeypatch.setattr(
        session_methods,
        "_request_agent_interrupt_async",
        lambda sid, agent: calls.append(("parent", (sid, agent))),
    )
    agent = object()
    session = {
        "history_lock": threading.Lock(),
        "interrupted_run_id": "",
        "interrupted_turn_id": "",
        "interrupt_seq": 0,
        "agent": agent,
    }
    frame = RunStartFrame(
        run_id="parent-run",
        turn_id="parent-turn",
        conversation_session_id="conversation-parent",
        prompt="start",
    )
    cancel_event = threading.Event()
    cancel_event.set()

    _watch_for_cancel("runtime-session", session, frame, cancel_event)

    assert calls == [
        (
            "children",
            {
                "conversation_session_id": "conversation-parent",
                "owner_run_id": "parent-run",
                "owner_turn_id": "parent-turn",
                "reason": "parent run cancelled",
            },
        ),
        ("parent", ("runtime-session", agent)),
    ]
    assert session["interrupted_run_id"] == "parent-run"
    assert session["interrupted_turn_id"] == "parent-turn"
    assert session["interrupt_seq"] == 1


def test_worker_imports_and_clears_runtime_capability() -> None:
    from agent_capabilities.credentials import capability_credentials

    conversation_id = "20260721_120000_taskhub"
    imported = _import_runtime_capabilities(
        [{
            "capability": "dovie.task_hub_assistant@1",
            "token": "worker-secret",
            "api_origin": "https://api.example.com",
            "conversation_id": conversation_id,
            "execution_participant_id": "agent-default",
            "expires_at": 4_000_000_000.0,
        }],
        conversation_session_id=conversation_id,
    )
    try:
        credential = capability_credentials.resolve(
            capability="dovie.task_hub_assistant@1",
            conversation_id=conversation_id,
        )
        assert credential.token == "worker-secret"
        assert imported == ["dovie.task_hub_assistant@1"]
        from model_tools import get_tool_definitions

        names = {
            tool["function"]["name"]
            for tool in get_tool_definitions(
                enabled_toolsets=["dovie_task_hub"],
                quiet_mode=True,
            )
        }
        assert names == {
            "task_hub_search",
            "task_hub_get_details",
            "task_hub_prepare_changes",
            "task_hub_list_my_tasks",
            "task_hub_open_source",
        }
    finally:
        _clear_runtime_capabilities(
            imported,
            conversation_session_id=conversation_id,
        )
    with pytest.raises(RuntimeError, match="not configured"):
        capability_credentials.resolve(
            capability="dovie.task_hub_assistant@1",
            conversation_id=conversation_id,
        )


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


def test_worker_session_restores_explicit_model_from_persisted_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A worker can recover an explicit model when the frame omits it."""
    from tui_gateway import server as _server

    from hermes_agent.composition.cli_session_store import open_cli_session_store

    session_id = "stored-session-with-explicit-model"
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(
        session_id=session_id,
        source="dovie",
        model="deepseek-v4-pro",
        model_config={
            "model_explicit": True,
            "reasoning_config": {"enabled": True, "effort": "xhigh"},
            "service_tier": "priority",
        },
    )
    persisted_session = db.sessions.get(session_id)
    assert isinstance(persisted_session, dict)
    assert isinstance(persisted_session["model_config"], str)

    sessions: dict[str, dict] = {}
    monkeypatch.setattr(_server, "_sessions", sessions)
    monkeypatch.setattr(_server, "_sessions_lock", threading.Lock())
    monkeypatch.setattr(_server, "_stdio_transport", _NoopTransport())
    monkeypatch.setattr(_server, "_db_for_stable_session", lambda _sid: db)

    sid, session = _ensure_worker_session(
        RunStartFrame(
            run_id="run-restore-model",
            turn_id="turn-restore-model",
            conversation_session_id=session_id,
            prompt="hello",
            params={"runtime_scope_key": "profile:test"},
        )
    )

    assert sessions[sid] is session
    assert session["model_override"] == {
        "model": "deepseek-v4-pro",
        "model_explicit": True,
    }
    assert session["create_reasoning_override"] == {
        "enabled": True,
        "effort": "xhigh",
    }
    assert session["create_service_tier_override"] == "priority"


def test_worker_session_turn_model_takes_precedence_over_persisted_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The control-plane turn selection is newer than the stored fallback."""
    from tui_gateway import server as _server

    from hermes_agent.composition.cli_session_store import open_cli_session_store

    session_id = "stored-session-with-new-turn-model"
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(
        session_id=session_id,
        source="dovie",
        model="stale-model",
        model_config={"model_explicit": True},
    )

    sessions: dict[str, dict] = {}
    monkeypatch.setattr(_server, "_sessions", sessions)
    monkeypatch.setattr(_server, "_sessions_lock", threading.Lock())
    monkeypatch.setattr(_server, "_stdio_transport", _NoopTransport())
    monkeypatch.setattr(_server, "_db_for_stable_session", lambda _sid: db)

    _sid, session = _ensure_worker_session(
        RunStartFrame(
            run_id="run-new-model",
            turn_id="turn-new-model",
            conversation_session_id=session_id,
            prompt="hello",
            params={
                "runtime_scope_key": "profile:test",
                "model": "turn-selected-model",
            },
        )
    )

    assert session["model_override"] == {
        "model": "turn-selected-model",
        "model_explicit": True,
    }


def test_team_leader_worker_hydrates_member_replies_as_observed_group_speech(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from tui_gateway import server as _server

    from hermes_agent.composition.cli_session_store import open_cli_session_store

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
