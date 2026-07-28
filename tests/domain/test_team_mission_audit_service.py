from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_team_mission.gateway.snapshot_methods import (
    _latest_team_mission_event_seq,
)


def test_audit_service_owns_append_list_snapshot_and_prune(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Mission",
            objective="Audit ownership",
            status="running",
            leader_session_id="team-session-1",
        )

        first = db.append_team_mission_structural_event(
            mission_id="mission-1",
            source_event={
                "type": "mission.node.created",
                "payload": {"mission_id": "mission-1"},
            },
            dedupe_key="node-created",
        )
        second = db.append_team_mission_structural_event(
            mission_id="mission-1",
            source_event={
                "type": "message.delta",
                "payload": {"mission_id": "mission-1", "text": "stream"},
            },
            dedupe_key="stream-delta",
        )

        assert [event["seq"] for event in db.team_mission_audit.list("mission-1")] == [
            first["seq"],
            second["seq"],
        ]
        assert _latest_team_mission_event_seq(db, "mission-1") == second["seq"]

        assert db.team_mission_audit.prune_source_event_types(
            mission_id="mission-1",
            source_event_types=("message.delta",),
        ) == 1
        remaining = db.team_mission_audit.list("mission-1")
        assert [event["payload"]["source_event_type"] for event in remaining] == [
            "mission.node.created"
        ]
    finally:
        db.close()
