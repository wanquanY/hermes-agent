from hermes_team_mission.read_model import build_team_mission_read_model


def test_read_model_normalizes_mission_and_node_statuses_to_backend_enums():
    read_model = build_team_mission_read_model(
        {
            "mission_id": "mission-1",
            "activity_id": "mission:mission-1",
            "snapshot_version": "mission:mission-1:seq:7",
            "last_event_seq": 7,
            "mission": {
                "mission_id": "mission-1",
                "conversation_id": "conversation-1",
                "team_id": "team-1",
                "title": "Build",
                "objective": "Build the feature",
                "status": "canceled",
                "created_at": 1,
                "updated_at": 2,
            },
            "conversation": {
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
                "team_id": "team-1",
                "title": "Team room",
            },
            "graph": {
                "nodes": [
                    {
                        "node_id": "node-final",
                        "mission_id": "mission-1",
                        "kind": "synthesizer",
                        "title": "Summarize",
                        "status": "canceled",
                        "metadata": {"task_id": "task-1"},
                        "created_at": 1,
                        "updated_at": 2,
                    }
                ],
                "edges": [],
            },
        }
    )

    assert read_model["schema_version"] == 2
    assert read_model["mission"]["status"] == "cancelled"
    assert read_model["conversation"]["conversation_session_id"] == "team-session-1"
    assert read_model["mission"]["conversation_session_id"] == "team-session-1"
    assert "conversation_id" not in read_model["conversation"]
    assert "conversation_id" not in read_model["mission"]
    assert read_model["nodes"][0]["status"] == "cancelled"
    assert read_model["nodes"][0]["runtime"]["node_status"] == "cancelled"
    assert read_model["nodes"][0]["kind"] == "synthesis"
    assert read_model["nodes"][0]["created_at"] == "1970-01-01T00:00:01Z"


def test_read_model_projects_empty_mission_as_conversation_shell():
    read_model = build_team_mission_read_model(
        {
            "mission_id": "",
            "activity_id": "",
            "snapshot_version": "conversation:conversation-only:no-mission",
            "last_event_seq": 0,
            "mission": {},
            "conversation": {
                "conversation_id": "conversation-only",
                "team_id": "team-1",
                "conversation_session_id": "team-session-conversation-only",
                "title": "Leader chat only",
                "status": "active",
                "created_at": 10,
                "updated_at": 20,
            },
            "graph": {"mission": {}, "nodes": [], "edges": []},
        }
    )

    assert read_model["mission"]["entity_kind"] == "conversation_shell"
    assert read_model["mission"]["mission_id"] == ""
    assert (
        read_model["mission"]["conversation_session_id"]
        == "team-session-conversation-only"
    )
    assert "conversation_id" not in read_model["mission"]
    assert read_model["mission"]["status"] == "draft"
    assert read_model["mission"]["conversation"]["status"] == "active"
    assert read_model["conversation"]["conversation_session_id"] == "team-session-conversation-only"
    assert read_model["nodes"] == []
    assert read_model["edges"] == []


def test_read_model_derives_node_dependencies_from_edges_and_nodes():
    read_model = build_team_mission_read_model(
        {
            "mission_id": "mission-deps",
            "activity_id": "mission:mission-deps",
            "mission": {
                "mission_id": "mission-deps",
                "conversation_id": "conversation-1",
                "team_id": "team-1",
                "title": "Dependency graph",
                "objective": "Exercise dependencies",
                "status": "running",
            },
            "conversation": {"conversation_id": "conversation-1", "conversation_session_id": "team-session-1"},
            "graph": {
                "nodes": [
                    {"node_id": "root", "mission_id": "mission-deps", "kind": "root", "status": "completed"},
                    {
                        "node_id": "worker",
                        "mission_id": "mission-deps",
                        "kind": "worker",
                        "status": "ready",
                        "depends_on": ["root"],
                    },
                    {"node_id": "verify", "mission_id": "mission-deps", "kind": "verifier", "status": "todo"},
                ],
                "edges": [
                    {"from_node_id": "root", "to_node_id": "worker", "kind": "depends_on"},
                    {"from_node_id": "worker", "to_node_id": "verify", "kind": "depends_on"},
                ],
            },
        }
    )

    nodes = {node["node_id"]: node for node in read_model["nodes"]}
    assert nodes["worker"]["depends_on"] == ["root"]
    assert nodes["verify"]["depends_on"] == ["worker"]
    assert read_model["edges"] == [
        {
            "edge_id": "",
            "mission_id": "",
            "from_node_id": "root",
            "to_node_id": "worker",
            "kind": "depends_on",
            "metadata": {},
            "created_at": "",
        },
        {
            "edge_id": "",
            "mission_id": "",
            "from_node_id": "worker",
            "to_node_id": "verify",
            "kind": "depends_on",
            "metadata": {},
            "created_at": "",
        },
    ]


def test_read_model_schema_version_is_present_for_minimal_snapshot():
    read_model = build_team_mission_read_model({})

    assert read_model["schema_version"] == 2
    assert read_model["mission"]["entity_kind"] == "conversation_shell"
    assert read_model["nodes"] == []
    assert read_model["edges"] == []
