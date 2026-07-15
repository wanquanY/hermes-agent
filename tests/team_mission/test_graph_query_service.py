from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store
from hermes_team_mission.read_models.graph_query import TeamMissionGraphQueryService


def test_cli_store_composes_explicit_team_mission_graph_query_service(
    tmp_path: Path,
) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.team_mission_graphs, TeamMissionGraphQueryService)
        assert "TeamMissionViewMixin" not in {
            base.__name__ for base in type(store).__mro__
        }
        assert not hasattr(store, "get_team_mission_graph")
        assert not hasattr(store, "get_team_mission_conversation_graph")
    finally:
        store.close()


def test_graph_query_service_projects_mission_and_conversation_graphs(
    tmp_path: Path,
) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.upsert_team_mission(
            mission_id="mission-visible",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Visible mission",
            objective="Build the visible graph",
            status="running",
            leader_session_id="team-session-1",
            created_at=1,
            updated_at=1,
            metadata={"task_id": "task-visible"},
        )
        store.upsert_team_mission_node(
            mission_id="mission-visible",
            node_id="root",
            kind="root",
            title="Root",
            status="completed",
        )
        store.upsert_team_mission_node(
            mission_id="mission-visible",
            node_id="worker",
            kind="worker",
            title="Worker",
            status="running",
        )
        store.upsert_team_mission_edge(
            mission_id="mission-visible",
            from_node_id="root",
            to_node_id="worker",
        )
        store.upsert_team_mission(
            mission_id="mission-member-chat",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Internal member chat",
            status="running",
            leader_session_id="team-session-1",
            created_at=2,
            updated_at=2,
            metadata={"member_chat_only": True},
        )
        store.messages.append("team-session-1", "user", "Inspect the graph")
        store.messages.append("team-session-1", "assistant", "Graph inspected")

        mission_graph = store.team_mission_graphs.get_team_mission_graph(
            "mission-visible"
        )
        conversation_graph = (
            store.team_mission_graphs.get_team_mission_conversation_graph(
                "conversation-1"
            )
        )

        assert mission_graph["mission"]["mission_id"] == "mission-visible"
        assert [node["node_id"] for node in mission_graph["nodes"]] == [
            "root",
            "worker",
        ]
        assert [frame["missionId"] for frame in conversation_graph["task_frames"]] == [
            "mission-visible"
        ]
        assert [node["node_id"] for node in conversation_graph["nodes"]] == [
            "mission-visible:root",
            "mission-visible:worker",
        ]
        assert [
            (edge["from_node_id"], edge["to_node_id"])
            for edge in conversation_graph["edges"]
        ] == [("mission-visible:root", "mission-visible:worker")]
        assert [
            message["text"] for message in conversation_graph["recent_messages"]
        ] == ["Inspect the graph", "Graph inspected"]
    finally:
        store.close()


def test_graph_query_service_rejects_empty_identifiers(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert store.team_mission_graphs.get_team_mission_graph("") == {}
        assert store.team_mission_graphs.get_team_mission_conversation_graph("") == {}
    finally:
        store.close()


def test_legacy_team_mission_view_mixin_module_is_deleted() -> None:
    assert not Path("hermes_team_mission/state/session_views.py").exists()
