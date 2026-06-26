from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_state import SessionDB
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.run_worker import EventFrame
from tui_gateway.services.run_control import record_event
from tui_gateway.services.worker_frame_router import WorkerFrameRouter


def _run_context(
    *,
    conversation_session_id: str = "conv-X",
    participant_id: str = "member:alice",
) -> RunContext:
    return RunContext(
        conversation_session_id=conversation_session_id,
        participant_id=participant_id,
        activity_id="activity-1",
        activity_kind="member_chat",
        execution_scope_key="member-chat:alice",
        control_home="/tmp/hermes-control",
        execution_home="/tmp/hermes-execution",
    )


def _db(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv-X", source="team_mission", transient=False)
    db.create_session("memberchat:Y", source="team_mission", transient=False)
    db.create_session("legacy-session", source="team_mission", transient=False)
    return db


def _frame(*, stored_session_id: str = "memberchat:Y", participant_id: str = "") -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "message.delta",
        "session_id": stored_session_id,
        "stored_session_id": stored_session_id,
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

    conv_events = db.list_run_events("conv-X", run_id="run-1")
    member_events = db.list_run_events("memberchat:Y", run_id="run-1")
    assert len(conv_events) == 1
    assert member_events == []
    assert conv_events[0]["stored_session_id"] == "conv-X"
    assert (conv_events[0]["payload"] or {})["run_context"]["conversation_session_id"] == "conv-X"


def test_record_event_with_run_context_stamps_participant_id_if_missing(tmp_path: Path):
    db = _db(tmp_path)

    record_event(_frame(), db=db, run_context=_run_context(participant_id="member:alice"))

    event = db.list_run_events("conv-X", run_id="run-1")[0]
    assert event["participant_id"] == "member:alice"


def test_record_event_with_run_context_preserves_existing_participant_id(tmp_path: Path):
    db = _db(tmp_path)

    record_event(
        _frame(participant_id="leader:foo"),
        db=db,
        run_context=_run_context(participant_id="member:alice"),
    )

    event = db.list_run_events("conv-X", run_id="run-1")[0]
    assert event["participant_id"] == "leader:foo"


def test_record_event_without_run_context_unchanged_legacy_path(tmp_path: Path):
    db = _db(tmp_path)

    record_event(_frame(stored_session_id="legacy-session"), db=db)

    legacy_events = db.list_run_events("legacy-session", run_id="run-1")
    conv_events = db.list_run_events("conv-X", run_id="run-1")
    assert len(legacy_events) == 1
    assert conv_events == []
    assert legacy_events[0]["stored_session_id"] == "legacy-session"
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
        stored_session_id="memberchat:Y",
        turn_id="turn-1",
        run_context_json=json.dumps(context.to_payload()),
    )

    await router.on_event("member-chat:alice", EventFrame(params=_frame()))

    assert captured["params"]["stored_session_id"] == "memberchat:Y"
    assert captured["run_context"] == context
