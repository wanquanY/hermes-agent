from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_team_mission.state.conversation import delete_team_mission_conversation
from hermes_team_mission.state.memory import (
    team_mission_binding_events,
    team_mission_binding_message_excerpt,
)


def test_cli_store_owns_team_mission_component_lifecycle(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.teams.upsert_agent_team(
            team_id="team-1",
            name="Native Team",
            lead_agent_profile_id="profile-leader",
            default_mode="supervised_mission",
            policy={},
        )
        db.teams.upsert_agent_team_member(
            member_id="member-leader",
            team_id="team-1",
            agent_profile_id="profile-leader",
            agent_profile_version_id="version-leader",
            role="lead",
            profile_name="Leader",
            profile_avatar="leader.png",
            capability_tags=["planning"],
        )
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Mission",
            objective="Exercise the canonical store",
            status="running",
            leader_session_id="team-session-1",
        )

        conversation = db.ensure_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            mission_id="mission-1",
            team_id="team-1",
            title="Team conversation",
        )
        db.add_mission_to_conversation(
            conversation_id="conversation-1",
            mission_id="mission-1",
            status="active",
        )

        assert conversation["conversation_session_id"] == "team-session-1"
        assert db.sessions.get("team-session-1")["source"] == "team_mission"
        participants = db.participants.list_conversation_participants("team-session-1")
        assert [participant["participant_id"] for participant in participants] == [
            "leader:conversation-1"
        ]
        assert db.activities.get_for_mission("mission-1")["status"] == "running"

        db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id="node-1",
            kind="worker",
            title="Worker",
            objective="Run",
            status="running",
        )
        db.runs.upsert(
            run_id="run-1",
            session_id="team-session-1",
            runtime_scope_key="team:conversation-1:member:member-leader",
            status="running",
        )
        binding = db.bind_team_mission_run(
            mission_id="mission-1",
            node_id="node-1",
            run_id="run-1",
            session_id="team-session-1",
            execution_session_id="runtime-1",
            runtime_scope_key="team:conversation-1:member:member-leader",
            role="worker",
        )
        db.append_team_mission_run_event(
            mission_id="mission-1",
            run_id="run-1",
            event={
                "type": "message.delta",
                "run_id": "run-1",
                "payload": {"text": "working"},
            },
        )
        db.messages.append("team-session-1", "assistant", "Durable result")

        assert [event["type"] for event in team_mission_binding_events(db, binding)] == [
            "message.delta"
        ]
        assert team_mission_binding_message_excerpt(db, binding) == "Durable result"

        db.set_conversation_mission_status(
            conversation_id="conversation-1",
            mission_id="mission-1",
            status="completed",
        )
        assert db.activities.get_for_mission("mission-1")["status"] == "completed"

        deleted = delete_team_mission_conversation(db, "conversation-1")
        assert deleted["deleted"] is True
        assert deleted["deleted_session_ids"] == ["team-session-1"]
        assert db.sessions.get("team-session-1") is None
        assert db.messages.list("team-session-1") == []
        assert db.runs.get("run-1") is None
    finally:
        db.close()
