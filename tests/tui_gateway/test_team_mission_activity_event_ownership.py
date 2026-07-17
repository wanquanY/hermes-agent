from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services.team_mission_activity_events import (
    activity_last_seq,
    mission_id_for_activity,
    mission_status_for_activity,
    uses_mission_activity_journal,
)


def test_activity_replay_queries_use_store_components(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Mission",
            objective="Verify activity replay ownership",
            status="running",
            leader_session_id="team-session-1",
        )
        db.activities.create(
            activity_id="act-team_dispatch-1",
            conversation_id="team-session-1",
            kind="team_dispatch",
        )
        db.activities.bind_to_mission(
            activity_id="act-team_dispatch-1",
            conversation_id="team-session-1",
            mission_id="mission-1",
        )

        db.runs.append_event(
            "team:mission:mission-1:events",
            {
                "type": "mission.node.created",
                "run_id": "run-1",
                "activity_id": "mission:mission-1",
                "payload": {"mission_id": "mission-1"},
            },
            activity_id="mission:mission-1",
        )
        db.runs.append_event(
            "team:mission:mission-1:events",
            {
                "type": "mission.node.updated",
                "activity_id": "mission:mission-1",
                "payload": {"mission_id": "mission-1"},
            },
            activity_id="mission:mission-1",
        )
        # A legacy projection may carry the same mission activity id inside a
        # node-local sequence domain. It must never participate in the mission
        # cursor after the dedicated activity ledger is authoritative.
        db.runs.append_event(
            "team:mission-1:node:legacy",
            {
                "type": "mission.node.created",
                "activity_id": "mission:mission-1",
                "payload": {"mission_id": "mission-1", "legacy": True},
            },
            activity_id="mission:mission-1",
        )
        db.runs.append_event(
            "chat-session-1",
            {
                "type": "message.complete",
                "run_id": "chat-run-1",
                "payload": {"text": "done"},
            },
        )

        assert mission_id_for_activity("act-team_dispatch-1", db=db) == "mission-1"
        assert uses_mission_activity_journal("mission:mission-1", db=db) is True
        assert mission_status_for_activity("mission:mission-1", db=db) == "running"
        assert activity_last_seq("mission:mission-1", db=db) == 2
        assert activity_last_seq("chat:chat-session-1", db=db) == 1
    finally:
        db.close()


def test_unknown_mission_does_not_select_mission_replay(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        assert uses_mission_activity_journal("mission:missing", db=db) is False
        assert mission_status_for_activity("mission:missing", db=db) == ""
        assert activity_last_seq("chat:missing", db=db) == 0
    finally:
        db.close()
