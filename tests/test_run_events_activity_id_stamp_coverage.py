from __future__ import annotations

import asyncio
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.run_worker import EventFrame, OutgoingFrame, RunTerminalFrame
from tui_gateway.services import run_control
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter
from hermes_agent.orchestration.worker_publish_bridge import WorkerPublishBridge


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _run_context(
    *,
    conversation_session_id: str = "team-session-1",
    participant_id: str = "leader:conversation-1",
    activity_id: str = "mission:mission-1",
    activity_kind: str = "mission",
    execution_scope_key: str = "team:mission-1:leader",
) -> RunContext:
    return RunContext(
        conversation_session_id=conversation_session_id,
        participant_id=participant_id,
        activity_id=activity_id,
        activity_kind=activity_kind,
        execution_scope_key=execution_scope_key,
        control_home="/tmp/hermes-control",
        execution_home="/tmp/hermes-execution",
    )


def _activity_rows(db: CliSessionStore) -> list[dict[str, Any]]:
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT session_id, run_id, event_type, activity_id FROM run_events ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def _last_activity_id(db: CliSessionStore, session_id: str) -> str:
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            "SELECT activity_id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    assert row is not None
    return str(row["activity_id"] or "")


def _setup_mission(db: CliSessionStore) -> None:
    db.sessions.create("team-session-1", source="team_mission", transient=False)
    db.sessions.create("leader-session-1", source="team_mission", transient=False)
    db.sessions.create("worker-session-1", source="team_mission", transient=False)
    db.sessions.create("verifier-session-1", source="team_mission", transient=False)
    db.sessions.create("synthesis-session-1", source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Do the work.",
        mode="supervised_mission",
        status="running",
        leader_session_id="team-session-1",
        metadata={"conversationTeamSessionId": "team-session-1", "conversation_id": "conversation-1"},
    )
    for node_id, kind, session_id, run_id in (
        ("root", "root", "leader-session-1", "leader-run-1"),
        ("worker", "worker", "worker-session-1", "worker-run-1"),
        ("verifier", "verifier", "verifier-session-1", "verifier-run-1"),
        ("synthesis", "synthesis", "synthesis-session-1", "synthesis-run-1"),
    ):
        db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id=node_id,
            kind=kind,
            title=node_id,
            objective=node_id,
            status="running",
        )
        db.bind_team_mission_run(
            mission_id="mission-1",
            node_id=node_id,
            run_id=run_id,
            session_id=session_id,
            execution_session_id=f"runtime-{node_id}",
            runtime_scope_key=f"team:mission-1:{node_id}",
            role=kind,
            metadata={"run_context_json": json.dumps(_run_context().to_payload())},
        )


def test_leader_submit_stamps_mission_or_chat_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        db.sessions.create("team-session-1", source="team_mission", transient=False)
        run_control.record_event(
            {"type": "message.start", "run_id": "leader-run-1", "payload": {"text": "go"}},
            db=db,
            run_context=_run_context(activity_id="mission:mission-1", activity_kind="mission"),
        )
        assert _last_activity_id(db, "team-session-1") == "mission:mission-1"

        db.sessions.create("chat-session-1", source="tui", transient=False)
        run_control.record_event(
            {"type": "message.start", "run_id": "chat-run-1", "payload": {"text": "hi"}},
            db=db,
            run_context=_run_context(
                conversation_session_id="chat-session-1",
                participant_id="agent:default",
                activity_id="chat:chat-session-1",
                activity_kind="chat",
                execution_scope_key="profile:default",
            ),
        )
        assert _last_activity_id(db, "chat-session-1") == "chat:chat-session-1"
    finally:
        db.close()


def test_leader_planning_node_dispatch_stamps_mission_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        _setup_mission(db)
        db.append_team_mission_run_event(
            mission_id="mission-1",
            run_id="leader-run-1",
            event={"type": "mission.node.run.bound", "payload": {"node_id": "root"}},
        )
        assert _last_activity_id(db, "leader-session-1") == "mission:mission-1"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_team_mission_node_message_complete_stamps_mission_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        _setup_mission(db)

        class _Sender:
            async def send(self, *_args: Any, **_kwargs: Any) -> bool:
                return True

        router = WorkerFrameRouter(
            sender=_Sender(),
            publish_event=lambda params, **kwargs: run_control.publish_recorded_event(
                params,
                db=db,
                **kwargs,
            ),
            publish_run_terminal=lambda **kwargs: run_control.publish_run_terminal_event(db=db, **kwargs),
        )
        router.record_run_start(
            scope_key="team:mission-1:worker",
            conversation_id="worker-session-1",
            run_id="worker-run-1",
            conversation_session_id="worker-session-1",
            turn_id="turn-worker",
            run_context_json=json.dumps(_run_context().to_payload()),
        )
        await router.on_event(
            "team:mission-1:worker",
            "worker-session-1",
            EventFrame(
                params={
                    "type": "message.complete",
                    "session_id": "runtime-worker",
                    "conversation_session_id": "worker-session-1",
                    "run_id": "worker-run-1",
                    "turn_id": "turn-worker",
                    "payload": {"status": "complete", "text": "done"},
                }
            ),
        )
        assert _last_activity_id(db, "team-session-1") == "mission:mission-1"
    finally:
        db.close()


def test_team_mission_live_conversation_mirror_is_disabled(tmp_path: Path) -> None:
    from hermes_team_mission.runtime import conversation_transcript

    assert hasattr(conversation_transcript, "append_user_task_message")
    assert "mirror_event_to_conversation" not in inspect.getsource(run_control.record_event)


@pytest.mark.asyncio
async def test_member_chat_dispatch_stamps_act_member_chat_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def publish_recorded_event(params: dict[str, Any], *_args: Any, **_kwargs: Any) -> list[Any]:
        calls.append(dict(params))
        return []

    fake_run_control = types.ModuleType("tui_gateway.services.run_control")
    fake_run_control.publish_recorded_event = publish_recorded_event
    original_module = sys.modules.get("tui_gateway.services.run_control")
    import tui_gateway.services as services_pkg

    original_attr = getattr(services_pkg, "run_control", None)
    sys.modules["tui_gateway.services.run_control"] = fake_run_control
    services_pkg.run_control = fake_run_control

    frames: list[OutgoingFrame] = []

    async def emit(frame: OutgoingFrame) -> None:
        frames.append(frame)

    bridge = WorkerPublishBridge(emit=emit, loop=asyncio.get_running_loop())
    try:
        bridge.install(
            conversation_session_id="team-session-1",
            run_context=_run_context(
                participant_id="member:alice",
                activity_id="act-member_chat:team-session-1:alice",
                activity_kind="member_chat",
                execution_scope_key="member-chat:team-session-1:alice",
            ),
        )
        fake_run_control.publish_recorded_event(
            {"type": "message.delta", "payload": {"delta": "hi"}}
        )
        await asyncio.sleep(0.05)
    finally:
        bridge.uninstall()
        if original_module is not None:
            sys.modules["tui_gateway.services.run_control"] = original_module
        else:
            sys.modules.pop("tui_gateway.services.run_control", None)
        if original_attr is not None:
            services_pkg.run_control = original_attr
        elif hasattr(services_pkg, "run_control"):
            delattr(services_pkg, "run_control")

    assert calls[0]["activity_id"] == "act-member_chat:team-session-1:alice"
    event_frames = [frame for frame in frames if isinstance(frame, EventFrame)]
    assert event_frames[0].params["activity_id"] == "act-member_chat:team-session-1:alice"


def test_prompt_submit_stamps_chat_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tui_gateway import server

    db = _db(tmp_path)
    try:
        db.sessions.create("chat-session-1", source="tui", transient=False)
        monkeypatch.setattr(server, "_db_for_stable_session", lambda _sid: db)
        monkeypatch.setattr(server, "write_json", lambda _obj: True)
        with server._sessions_lock:  # noqa: SLF001
            server._sessions["runtime-1"] = {  # noqa: SLF001
                "session_key": "chat-session-1",
                "active_run_id": "chat-run-1",
                "active_turn_id": "turn-chat",
                "active_runtime_scope_key": "chat-session-1",
            }
        try:
            server._emit("message.complete", "runtime-1", {"status": "complete"})
        finally:
            with server._sessions_lock:  # noqa: SLF001
                server._sessions.pop("runtime-1", None)  # noqa: SLF001
        assert _last_activity_id(db, "chat-session-1") == "chat:chat-session-1"
    finally:
        db.close()


def test_approval_event_stamps_mission_prefix(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        db.sessions.create("team-session-1", source="team_mission", transient=False)
        run_control.record_event(
            {
                "type": "approval.request",
                "run_id": "leader-run-1",
                "payload": {"request_id": "approval-1", "message": "approve?"},
            },
            db=db,
            run_context=_run_context(),
        )
        assert _last_activity_id(db, "team-session-1") == "mission:mission-1"
    finally:
        db.close()


def test_tool_event_stamps_propagates_activity_id_from_run_context(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        db.sessions.create("team-session-1", source="team_mission", transient=False)
        run_control.record_event(
            {
                "type": "tool.start",
                "run_id": "leader-run-1",
                "payload": {"tool": "terminal", "name": "terminal"},
            },
            db=db,
            run_context=_run_context(),
        )
        assert _last_activity_id(db, "team-session-1") == "mission:mission-1"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_no_run_events_row_with_null_activity_id_after_full_mission_run(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        _setup_mission(db)

        run_control.record_event(
            {"type": "message.start", "run_id": "leader-run-1", "turn_id": "turn-leader", "payload": {}},
            db=db,
            run_context=_run_context(),
        )
        run_control.record_event(
            {
                "type": "approval.request",
                "run_id": "leader-run-1",
                "turn_id": "turn-leader",
                "payload": {"request_id": "approval-1"},
            },
            db=db,
            run_context=_run_context(),
        )
        db.append_team_mission_run_event(
            mission_id="mission-1",
            run_id="worker-run-1",
            event={"type": "mission.node.run.bound", "payload": {"node_id": "worker"}},
        )

        class _Sender:
            async def send(self, *_args: Any, **_kwargs: Any) -> bool:
                return True

        router = WorkerFrameRouter(
            sender=_Sender(),
            publish_event=lambda params, **kwargs: run_control.publish_recorded_event(
                params,
                db=db,
                **kwargs,
            ),
            publish_run_terminal=lambda **kwargs: run_control.publish_run_terminal_event(db=db, **kwargs),
        )
        for run_id, session_id, scope in (
            ("worker-run-1", "worker-session-1", "team:mission-1:worker"),
            ("verifier-run-1", "verifier-session-1", "team:mission-1:verifier"),
        ):
            router.record_run_start(
                scope_key=scope,
                conversation_id=session_id,
                run_id=run_id,
                conversation_session_id=session_id,
                turn_id=f"turn-{run_id}",
                run_context_json=json.dumps(_run_context().to_payload()),
            )
            await router.on_event(
                scope,
                session_id,
                EventFrame(
                    params={
                        "type": "message.complete",
                        "session_id": f"runtime-{run_id}",
                        "conversation_session_id": session_id,
                        "run_id": run_id,
                        "turn_id": f"turn-{run_id}",
                        "payload": {"status": "complete", "text": f"{run_id} done"},
                    }
                ),
            )

        router.record_run_start(
            scope_key="team:mission-1:worker",
            conversation_id="worker-session-1",
            run_id="crashed-run-1",
            conversation_session_id="worker-session-1",
            turn_id="turn-crashed",
            run_context_json=json.dumps(_run_context().to_payload()),
        )
        await router.on_run_terminal(
            "team:mission-1:worker",
            "worker-session-1",
            RunTerminalFrame(
                run_id="crashed-run-1",
                status="failed",
                conversation_session_id="worker-session-1",
                turn_id="turn-crashed",
                message="worker failed",
            ),
        )

        null_rows = [row for row in _activity_rows(db) if not row["activity_id"]]
        assert null_rows == []
    finally:
        db.close()
