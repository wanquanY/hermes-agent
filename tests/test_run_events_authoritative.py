from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from hermes_state import SessionDB
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


def _install_db(monkeypatch: Any, tmp_path: Path) -> SessionDB:
    conversation_render_snapshot = importlib.import_module(
        "tui_gateway.methods.conversation_render_snapshot"
    )
    session_history = importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
    monkeypatch.setattr(session_history, "_get_db", lambda: db)
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    return db


def _seed_team_conversation(db: SessionDB, *, mission_status: str = "running") -> None:
    db.create_session(session_id="team-session-1", source="team_mission")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team conversation",
        active_mission_id="mission-1",
        created_at=1,
        updated_at=2,
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        objective="Verify run_events authority.",
        status=mission_status,
        leader_session_id="team-session-1",
    )


def _message_complete(
    run_id: str,
    text: str,
    *,
    seq: int = 0,
    participant_id: str = "",
    message_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text, "status": "complete"}
    if participant_id:
        payload["participant_id"] = participant_id
    if message_id:
        payload["message_id"] = message_id
    return {
        "type": "message.complete",
        "session_id": f"runtime-{run_id}",
        "stored_session_id": "team-session-1",
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "runtime_scope_key": "team:conversation-1",
        "seq": seq,
        "payload": payload,
    }


def _render_team() -> dict[str, Any]:
    response = server._methods["team_mission.conversation.render"](
        "run-events-authoritative-render",
        {"conversation_id": "conversation-1", "includeRunEvents": True, "limit": 100},
    )
    assert "error" not in response
    return response["result"]


def test_run_events_seq_monotonic_across_leader_and_member_runs_same_conv(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        _seed_team_conversation(db)

        db.append_run_event("team-session-1", _message_complete("run-leader", "leader", seq=1))
        db.append_run_event("team-session-1", _message_complete("run-member", "member", seq=1))

        events = db.list_run_events("team-session-1")
        assert [event["seq"] for event in events] == [1, 2]
        assert [event["run_id"] for event in events] == ["run-leader", "run-member"]
    finally:
        db.close()


def test_team_render_does_not_synthesize_messages_from_run_events(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        _seed_team_conversation(db)
        db.append_run_event(
            "team-session-1",
            _message_complete("run-member", "rendered from run_events", participant_id="member:builder"),
        )

        result = _render_team()

        assert result["messages"] == []
    finally:
        db.close()


def test_team_mission_events_not_in_render_snapshot_messages(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        _seed_team_conversation(db)
        db.append_team_mission_structural_event(
            mission_id="mission-1",
            source_event={
                "type": "message.complete",
                "run_id": "audit-run",
                "seq": 1,
                "payload": {"text": "audit event must not render", "status": "complete"},
            },
            dedupe_key="audit-message",
        )

        result = _render_team()

        assert result["messages"] == []
        assert "teamMissionEvents" not in result
        assert "team_mission_events" not in result
    finally:
        db.close()


def test_team_mission_events_still_appended_for_audit(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        _seed_team_conversation(db)
        db.append_team_mission_structural_event(
            mission_id="mission-1",
            source_event={
                "type": "mission.plan.approval.requested",
                "seq": 1,
                "payload": {"status": "waiting_approval"},
            },
            dedupe_key="approval-requested",
        )

        response = server._methods["team_mission.events"](
            "run-events-authoritative-audit",
            {"mission_id": "mission-1"},
        )

        assert "error" not in response
        assert response["result"]["audit_only"] is True
        assert [event["payload"]["source_event_type"] for event in response["result"]["events"]] == [
            "mission.plan.approval.requested"
        ]
    finally:
        db.close()


def test_run_events_do_not_duplicate_explicit_transcript_messages_in_render(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        _seed_team_conversation(db)
        db.append_message(
            "team-session-1",
            role="assistant",
            content="same final",
            metadata={"transcript_activity_kind": "mission_summary"},
        )
        db.append_run_event(
            "team-session-1",
            _message_complete("run-a", "same final", message_id="msg-final"),
        )
        db.append_run_event(
            "team-session-1",
            _message_complete("run-b", "same final", message_id="msg-final"),
        )

        result = _render_team()

        assert [message["text"] for message in result["messages"]] == ["same final"]
    finally:
        db.close()


def test_event_ordering_by_run_events_seq(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        _seed_team_conversation(db)
        for text in ("first", "second", "third"):
            db.append_message(
                "team-session-1",
                role="assistant",
                content=text,
                metadata={"transcript_activity_kind": "mission_summary"},
            )
        for run_id, text in (
            ("run-leader", "first"),
            ("run-member", "second"),
            ("run-verifier", "third"),
        ):
            db.append_run_event("team-session-1", _message_complete(run_id, text))

        events = db.list_run_events("team-session-1")
        result = _render_team()

        assert [event["seq"] for event in events] == [1, 2, 3]
        assert [message["text"] for message in result["messages"]] == ["first", "second", "third"]
    finally:
        db.close()
