"""Phase D4 — TeamMissionRepoImpl concrete behavior (spec §4.4)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_agent.repositories import (
    Activity,
    ActivitySpec,
    EdgeSpec,
    Mission,
    MissionGraph,
    MissionNode,
    MissionSpec,
    NodeSpec,
    RunConversationBinding,
    TeamMissionRepo,
    TeamMissionRepoImpl,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE team_missions (
            mission_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL DEFAULT '',
            title TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            plan_json TEXT,
            metadata_json TEXT
        );
        CREATE TABLE team_mission_nodes (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL,
            bound_run_id TEXT NOT NULL DEFAULT '',
            plan_json TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (mission_id, node_id)
        );
        CREATE TABLE team_mission_edges (
            mission_id TEXT NOT NULL,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'sequence',
            PRIMARY KEY (mission_id, from_node_id, to_node_id)
        );
        CREATE TABLE team_mission_run_bindings (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            bound_at REAL NOT NULL,
            PRIMARY KEY (mission_id, node_id)
        );
        CREATE TABLE team_mission_conversations (
            conversation_id TEXT PRIMARY KEY,
            conversation_session_id TEXT NOT NULL
        );
        CREATE TABLE v3_activities (
            activity_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            activity_seq INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            target_id TEXT NOT NULL DEFAULT '',
            prompt_summary TEXT,
            result_summary TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL DEFAULT 0,
            completed_at REAL,
            metadata_json TEXT
        );
        CREATE TABLE activity_commands (
            command_id TEXT PRIMARY KEY,
            activity_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            intent_at REAL NOT NULL,
            state TEXT NOT NULL,
            state_changed_at REAL NOT NULL,
            result_event_id INTEGER,
            error_reason TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL DEFAULT '',
            timestamp REAL NOT NULL DEFAULT 0,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE seq_counter (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO sessions (id, source) VALUES ('s1', 'test')")
    conn.commit()
    return conn


def test_impl_is_structural_team_mission_repo():
    repo = TeamMissionRepoImpl(_make_conn())
    assert isinstance(repo, TeamMissionRepo)


def test_production_schema_supports_v3_activity_repository(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s1", "test")
        repo = TeamMissionRepoImpl(db._conn)

        activity = repo.append_activity(
            "s1",
            ActivitySpec(
                activity_id="a-production-schema",
                session_id="s1",
                kind="async_agent_dispatch",
                target_id="agent-1",
                prompt_summary="dispatch agent",
            ),
        )

        row = db._conn.execute(
            """
            SELECT activity_id, session_id, kind, activity_seq, status, target_id
              FROM v3_activities
             WHERE activity_id = ?
            """,
            ("a-production-schema",),
        ).fetchone()
        assert row is not None
        assert row["activity_id"] == activity.activity_id
        assert row["session_id"] == "s1"
        assert row["kind"] == "async_agent_dispatch"
        assert row["activity_seq"] == activity.activity_seq
        assert row["status"] == "pending"
        assert row["target_id"] == "agent-1"
    finally:
        db.close()


def test_create_mission_and_get_graph_empty():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    mission = repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1", title="Mission 1"))
    assert isinstance(mission, Mission)

    graph = repo.get_graph("m1")
    assert isinstance(graph, MissionGraph)
    assert graph.mission.mission_id == "m1"
    assert graph.mission.title == "Mission 1"
    assert graph.nodes == ()
    assert graph.edges == ()


def test_get_graph_missing_returns_none():
    repo = TeamMissionRepoImpl(_make_conn())
    assert repo.get_graph("no-such") is None


def test_add_nodes_and_edges_and_read_graph():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    repo.add_node(NodeSpec(mission_id="m1", node_id="n1", title="Plan"))
    repo.add_node(NodeSpec(mission_id="m1", node_id="n2", title="Execute"))
    repo.add_edge(EdgeSpec(mission_id="m1", from_node_id="n1", to_node_id="n2"))

    graph = repo.get_graph("m1")
    assert [n.node_id for n in graph.nodes] == ["n1", "n2"]
    assert [(e.from_node_id, e.to_node_id) for e in graph.edges] == [("n1", "n2")]


def test_bind_run_updates_node_and_binding_row():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    repo.add_node(NodeSpec(mission_id="m1", node_id="n1"))

    repo.bind_run("m1", "n1", "run-A")

    node = repo.get_node("m1", "n1")
    assert node is not None
    assert node.bound_run_id == "run-A"

    row = conn.execute(
        "SELECT run_id FROM team_mission_run_bindings WHERE mission_id='m1' AND node_id='n1'"
    ).fetchone()
    assert row["run_id"] == "run-A"


def test_get_run_conversation_binding_returns_session_projection_target():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    repo.create_mission(
        "s1",
        MissionSpec(
            mission_id="m1",
            session_id="s1",
            metadata={"conversation_id": "conversation-1"},
        ),
    )
    conn.execute(
        """
        UPDATE team_missions
           SET conversation_id = 'conversation-1'
         WHERE mission_id = 'm1'
        """
    )
    conn.execute(
        """
        INSERT INTO team_mission_conversations (conversation_id, conversation_session_id)
        VALUES ('conversation-1', 'team-session-1')
        """
    )
    repo.add_node(NodeSpec(mission_id="m1", node_id="n1"))
    repo.bind_run("m1", "n1", "run-A")

    binding = repo.get_run_conversation_binding("run-A")

    assert binding == RunConversationBinding(
        conversation_session_id="team-session-1",
        conversation_scope_key="team:conversation-1:leader-conversation",
        mission_is_terminal=False,
    )


def test_update_node_status_transitions():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    repo.add_node(NodeSpec(mission_id="m1", node_id="n1"))
    repo.update_node_status("m1", "n1", "running")
    node = repo.get_node("m1", "n1")
    assert node.status == "running"


def test_append_activity_shares_seq_counter_domain():
    """spec §6.5 — activity_seq is drawn from the same SeqAllocator as run_events."""
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    # Simulate a canonical event first. The counter is authoritative; runtime
    # MAX(run_events.seq) backfill has been retired.
    conn.execute(
        "INSERT INTO run_events (session_id, seq, event_type, timestamp) VALUES ('s1', 1, 'message.start', 0)"
    )
    conn.execute(
        "INSERT INTO seq_counter (session_id, next_seq, updated_at) VALUES ('s1', 2, 0)"
    )
    a1 = repo.append_activity(
        "s1",
        ActivitySpec(activity_id="a1", session_id="s1", kind="async_agent_dispatch"),
    )
    a2 = repo.append_activity(
        "s1",
        ActivitySpec(activity_id="a2", session_id="s1", kind="team_mission_activity"),
    )
    # Both activities pick up strictly monotonic seqs continuing past run_events.seq=1.
    assert a1.activity_seq >= 2
    assert a2.activity_seq == a1.activity_seq + 1


def test_append_activity_rejects_unknown_kind():
    repo = TeamMissionRepoImpl(_make_conn())
    with pytest.raises(ValueError):
        repo.append_activity(
            "s1",
            ActivitySpec(activity_id="a1", session_id="s1", kind="chat"),  # type: ignore[arg-type]
        )


def test_list_activities_orders_by_seq_and_supports_cursor():
    repo = TeamMissionRepoImpl(_make_conn())
    for i, kind in enumerate(
        ("async_agent_dispatch", "team_mission_activity", "async_team_dispatch"),
        start=1,
    ):
        repo.append_activity(
            "s1",
            ActivitySpec(activity_id=f"a{i}", session_id="s1", kind=kind),  # type: ignore[arg-type]
        )
    all_ = repo.list_activities("s1")
    assert [a.activity_id for a in all_] == ["a1", "a2", "a3"]
    after_first = repo.list_activities("s1", after_seq=all_[0].activity_seq)
    assert [a.activity_id for a in after_first] == ["a2", "a3"]


def test_list_activities_filters_by_kind():
    repo = TeamMissionRepoImpl(_make_conn())
    repo.append_activity("s1", ActivitySpec(activity_id="a1", session_id="s1", kind="async_agent_dispatch"))
    repo.append_activity("s1", ActivitySpec(activity_id="a2", session_id="s1", kind="team_mission_activity"))
    got = repo.list_activities("s1", kinds={"async_agent_dispatch"})
    assert [a.activity_id for a in got] == ["a1"]


def test_update_activity_status_sets_completed_at_on_terminal():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    repo.append_activity("s1", ActivitySpec(activity_id="a1", session_id="s1", kind="async_agent_dispatch"))
    updated = repo.update_activity_status("a1", "completed")
    assert isinstance(updated, Activity)
    assert updated.status == "completed"
    assert updated.completed_at is not None


def test_update_activity_status_missing_raises():
    repo = TeamMissionRepoImpl(_make_conn())
    with pytest.raises(LookupError):
        repo.update_activity_status("nope", "completed")


def test_activity_commands_are_team_mission_repo_owned():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)

    created = repo.insert_activity_command(
        command_id="cmd-1",
        activity_id="activity-1",
        kind="start",
        payload={"mission": "m1"},
        metadata={"source": "test"},
    )
    duplicate = repo.insert_activity_command(
        command_id="cmd-1",
        activity_id="activity-1",
        kind="start",
    )

    assert duplicate == {}
    assert created["command_id"] == "cmd-1"
    assert created["state"] == "accepted"
    assert created["payload"] == {"mission": "m1"}
    assert created["metadata"] == {"source": "test"}
    assert repo.get_activity_command("cmd-1")["kind"] == "start"
    assert [row["command_id"] for row in repo.list_pending_activity_commands()] == ["cmd-1"]
    assert [
        row["command_id"] for row in repo.list_activity_commands_for_activity("activity-1")
    ] == ["cmd-1"]

    dispatched = repo.update_activity_command_state(
        "cmd-1",
        next_state="dispatched",
        result_event_id=42,
    )
    assert dispatched["state"] == "dispatched"
    assert dispatched["result_event_id"] == 42
    assert repo.update_activity_command_state("cmd-1", next_state="accepted") == {}


def test_migrate_legacy_activities_kind_check_allows_mission_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE activities (
            activity_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            parent_activity_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('chat', 'agent_dispatch', 'team_dispatch', 'member_chat')),
            target_profile_id TEXT,
            target_team_id TEXT,
            target_mission_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
            prompt_summary TEXT,
            result_summary TEXT,
            result_json TEXT,
            started_at REAL,
            completed_at REAL,
            notify_parent INTEGER NOT NULL DEFAULT 1,
            read_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO activities (
            activity_id, conversation_id, kind, status, created_at, updated_at
        ) VALUES ('a1', 'c1', 'chat', 'pending', 1, 1);
        """
    )
    repo = TeamMissionRepoImpl(conn)

    assert repo.migrate_legacy_activities_kind_mission_check() is True

    original = conn.execute("SELECT activity_id, kind FROM activities WHERE activity_id = 'a1'").fetchone()
    assert dict(original) == {"activity_id": "a1", "kind": "chat"}
    conn.execute(
        """
        INSERT INTO activities (
            activity_id, conversation_id, kind, status, created_at, updated_at
        ) VALUES ('mission:1', 'c1', 'mission', 'running', 2, 2)
        """
    )
    mission = conn.execute(
        "SELECT activity_id, kind FROM activities WHERE activity_id = 'mission:1'"
    ).fetchone()
    assert dict(mission) == {"activity_id": "mission:1", "kind": "mission"}
    assert repo.migrate_legacy_activities_kind_mission_check() is False
