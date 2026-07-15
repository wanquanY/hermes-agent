from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_team_mission.context.worker_context import prior_node_attempts


def test_prior_node_attempts_reads_bindings_and_terminal_events_from_components(
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
        db.upsert_team_mission_node(
            mission_id="mission-1",
            node_id="node-1",
            kind="worker",
            title="Worker",
            status="failed",
        )
        db.runs.upsert(
            run_id="run-1",
            session_id="team-session-1",
            status="failed",
        )
        db.bind_team_mission_run(
            mission_id="mission-1",
            node_id="node-1",
            run_id="run-1",
            session_id="team-session-1",
            execution_session_id="execution-1",
            role="worker",
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "run.failed",
                "run_id": "run-1",
                "payload": {
                    "status": "failed",
                    "error": "worker failed",
                },
            },
        )

        attempts = prior_node_attempts(db, "mission-1", "node-1")

        assert attempts == [
            {
                "run_id": "run-1",
                "status": "failed",
                "role": "worker",
                "summary": "worker failed",
                "terminal_event": "run.failed",
                "terminal_status": "failed",
            }
        ]
    finally:
        db.close()
