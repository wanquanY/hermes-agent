from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_conversation_deliverable_projection_combines_message_and_memory_artifacts(
    tmp_path: Path,
) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.sessions.create("conversation-session-1", source="team_mission")
        store.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Mission",
            status="completed",
        )
        store.messages.append(
            "conversation-session-1",
            role="assistant",
            content="Final answer",
            metadata={
                "team_mission": {
                    "kind": "final_deliverable",
                    "mission_id": "mission-1",
                    "task_id": "task-1",
                    "source_run_id": "run-1",
                }
            },
        )
        store.upsert_team_mission_memory_item(
            memory_id="memory-1",
            team_id="team-1",
            mission_id="mission-1",
            conversation_session_id="conversation-session-1",
            task_id="task-1",
            content="Artifact",
            artifact_refs=[{"path": "/tmp/report.md", "title": "report.md"}],
        )

        projection = store.team_mission_conversation_deliverables.project(
            {"conversation_session_id": "conversation-session-1"},
            [{"mission_id": "mission-1"}],
        )

        assert projection["last_message_preview"] == "Final answer"
        assert len(projection["final_deliverables"]) == 1
        assert projection["final_deliverables"][0]["mission_id"] == "mission-1"
        assert projection["artifact_refs"] == [
            {"path": "/tmp/report.md", "title": "report.md"}
        ]
        assert projection["final_deliverables"][0]["artifact_refs"] == [
            {"path": "/tmp/report.md", "title": "report.md"}
        ]
    finally:
        store.close()


def test_projection_without_missions_keeps_last_message_only(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.sessions.create("conversation-session-1", source="team_mission")
        store.messages.append(
            "conversation-session-1",
            role="user",
            content="Latest request",
        )

        projection = store.team_mission_conversation_deliverables.project(
            {"conversation_session_id": "conversation-session-1"},
            [],
        )

        assert projection["last_message_preview"] == "Latest request"
        assert projection["final_deliverables"] == []
        assert projection["artifact_refs"] == []
    finally:
        store.close()
