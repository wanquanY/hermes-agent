from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.run_worker import EventFrame
from tui_gateway.services.run_control import record_event
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter


def _run_context(
    *,
    conversation_session_id: str = "conv-X",
    participant_id: str = "member:alice",
    activity_id: str = "act-member_chat:conv-X:alice",
    activity_kind: str = "member_chat",
    execution_scope_key: str = "member-chat:alice",
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


def _db(tmp_path: Path) -> CliSessionStore:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv-X", source="team_mission", transient=False)
    db.sessions.create("memberchat:Y", source="team_mission", transient=False)
    db.sessions.create("team:mission-1:node:root", source="team_mission", transient=False)
    db.sessions.create("legacy-session", source="team_mission", transient=False)
    return db


def _frame(*, conversation_session_id: str = "memberchat:Y", participant_id: str = "") -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "message.delta",
        "session_id": conversation_session_id,
        "conversation_session_id": conversation_session_id,
        "run_id": "run-1",
        "turn_id": "turn-1",
        "seq": 1,
        "payload": {"delta": "hi", "mode": "append"},
    }
    if participant_id:
        frame["participant_id"] = participant_id
    return frame


def test_record_event_with_run_context_forces_conversation_session_id(tmp_path: Path):
    db = _db(tmp_path)

    record_event(_frame(), db=db, run_context=_run_context())

    conv_events = db.runs.list_events("conv-X", run_id="run-1")
    member_events = db.runs.list_events("memberchat:Y", run_id="run-1")
    assert len(conv_events) == 1
    assert member_events == []
    assert conv_events[0]["conversation_session_id"] == "conv-X"
    assert (conv_events[0]["payload"] or {})["run_context"]["conversation_session_id"] == "conv-X"


def test_record_event_with_node_run_context_preserves_node_runtime_session(tmp_path: Path):
    db = _db(tmp_path)
    context = _run_context(
        participant_id="leader:conv-X",
        activity_id="act-node:mission-1:team-mission:mission-1:root",
        activity_kind="mission",
        execution_scope_key="profile:agent-default",
    )

    record_event(
        _frame(conversation_session_id="team:mission-1:node:root"),
        db=db,
        run_context=context,
    )

    node_events = db.runs.list_events("team:mission-1:node:root", run_id="run-1")
    conv_events = db.runs.list_events("conv-X", run_id="run-1")
    assert len(node_events) == 1
    assert conv_events == []
    assert node_events[0]["conversation_session_id"] == "team:mission-1:node:root"
    assert node_events[0]["activity_id"] == "act-node:mission-1:team-mission:mission-1:root"
    assert (node_events[0]["payload"] or {})["run_context"]["conversation_session_id"] == "conv-X"


def test_record_event_with_run_context_stamps_participant_id_if_missing(tmp_path: Path):
    db = _db(tmp_path)

    record_event(_frame(), db=db, run_context=_run_context(participant_id="member:alice"))

    event = db.runs.list_events("conv-X", run_id="run-1")[0]
    assert event["participant_id"] == "member:alice"


def test_record_event_with_run_context_preserves_existing_participant_id(tmp_path: Path):
    db = _db(tmp_path)

    record_event(
        _frame(participant_id="leader:foo"),
        db=db,
        run_context=_run_context(participant_id="member:alice"),
    )

    event = db.runs.list_events("conv-X", run_id="run-1")[0]
    assert event["participant_id"] == "leader:foo"


def test_record_event_without_run_context_unchanged_legacy_path(tmp_path: Path):
    db = _db(tmp_path)

    record_event(_frame(conversation_session_id="legacy-session"), db=db)

    legacy_events = db.runs.list_events("legacy-session", run_id="run-1")
    conv_events = db.runs.list_events("conv-X", run_id="run-1")
    assert len(legacy_events) == 1
    assert conv_events == []
    assert legacy_events[0]["conversation_session_id"] == "legacy-session"
    assert "run_context" not in (legacy_events[0].get("payload") or {})


@pytest.mark.asyncio
async def test_worker_router_passes_run_context_from_spawn_payload():
    captured: dict[str, Any] = {}

    class _Sender:
        async def send(self, _scope_key: str, _frame: Any) -> bool:
            return True

    def _publish_event(params: dict[str, Any], *, run_context: RunContext | None = None):
        captured["params"] = params
        captured["run_context"] = run_context
        return []

    router = WorkerFrameRouter(
        sender=_Sender(),
        publish_event=_publish_event,
        publish_run_terminal=lambda **_kwargs: None,
    )
    context = _run_context()
    router.record_run_start(
        scope_key="member-chat:alice",
        run_id="run-1",
        conversation_session_id="memberchat:Y",
        turn_id="turn-1",
        run_context_json=json.dumps(context.to_payload()),
    )

    await router.on_event("member-chat:alice", EventFrame(params=_frame()))

    assert captured["params"]["conversation_session_id"] == "memberchat:Y"
    assert captured["run_context"] == context
