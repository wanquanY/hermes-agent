from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_team_mission.runtime.approval_observer import _missions_for_session_key


def test_approval_observer_resolves_missions_through_team_read_model(
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Mission",
            status="running",
            leader_session_id="team-session-1",
        )
        db.ensure_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            mission_id="mission-1",
            team_id="team-1",
            title="Conversation",
        )
        db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id="node-1",
            kind="worker",
            title="Worker",
            status="running",
        )
        db.bind_team_mission_run(
            mission_id="mission-1",
            node_id="node-1",
            run_id="run-1",
            session_id="member-session-1",
            execution_session_id="execution-1",
            runtime_scope_key="team:conversation-1:member:member-1",
            role="worker",
        )

        for session_key in (
            "team-session-1",
            "member-session-1",
            "execution-1",
            "team:conversation-1:member:member-1",
        ):
            assert _missions_for_session_key(db, session_key) == ["mission-1"]
        assert _missions_for_session_key(db, "missing") == []
    finally:
        db.close()
