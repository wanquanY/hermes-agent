from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.methods import conversation_render_snapshot as render_snapshot


def test_render_snapshot_reads_state_through_store_components(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(
            "team-session-1",
            source="team_mission",
            conversation_kind="team",
        )
        db.participants.upsert_conversation_participant(
            conversation_session_id="team-session-1",
            participant_id="leader:conversation-1",
            role="leader",
            display_name="Leader",
        )
        db.activities.ensure_mission(
            conversation_id="team-session-1",
            mission_id="mission-1",
            status="running",
        )
        db.runs.append_event(
            "team-session-1",
            {"type": "message.delta", "run_id": "run-1", "payload": {}},
        )
        db.runs.append_event(
            "team-session-1",
            {"type": "message.complete", "run_id": "run-1", "payload": {}},
        )
        monkeypatch.setattr(render_snapshot, "_get_db", lambda: db)

        assert render_snapshot._route_kind_from_session_index(db, "team-session-1") == "team"
        assert [
            participant["participant_id"]
            for participant in render_snapshot._participants_for_session("team-session-1")
        ] == ["leader:conversation-1"]
        assert [
            activity["target_mission_id"]
            for activity in render_snapshot._mission_activities_for_session("team-session-1")
        ] == ["mission-1"]
        assert render_snapshot._run_event_session_last_seq(db, "team-session-1") == 2
        assert render_snapshot._run_event_activity_last_seq(db, "chat:team-session-1") == 2
    finally:
        db.close()
