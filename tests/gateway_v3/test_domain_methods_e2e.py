"""Phase G — end-to-end dispatch through the real Repo + orchestrator wiring."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods import (
    run_methods,
    session_methods,
    team_mission_methods,
)
from hermes_agent.orchestration import TeamMissionOrchestrator
from hermes_agent.repositories import (
    CanonicalEventSpec,
    EdgeSpec,
    MissionSpec,
    NodeSpec,
    RunRepoImpl,
    RunSpec,
    SessionRepoImpl,
    SessionSpec,
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
            title TEXT,
            display_title TEXT,
            display_title_source TEXT,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            parent_session_id TEXT,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY,
            owner_agent_profile_id TEXT NOT NULL DEFAULT '',
            owner_profile_version_id TEXT NOT NULL DEFAULT '',
            runtime_scope_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            session_kind TEXT NOT NULL DEFAULT '',
            conversation_kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',
            running INTEGER NOT NULL DEFAULT 0,
            waiting_approval INTEGER NOT NULL DEFAULT 0,
            active_run_id TEXT NOT NULL DEFAULT '',
            active_runtime_session_id TEXT NOT NULL DEFAULT '',
            pending_approval_count INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            last_activity REAL
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            runtime_scope_key TEXT,
            turn_id TEXT,
            runtime_session_id TEXT,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            last_seq INTEGER DEFAULT 0,
            terminal_seq INTEGER NOT NULL DEFAULT 0,
            terminal_degraded INTEGER NOT NULL DEFAULT 0,
            terminal_cause TEXT NOT NULL DEFAULT '',
            error TEXT,
            metadata_json TEXT
        );
        CREATE TABLE run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            turn_id TEXT,
            timestamp REAL NOT NULL,
            payload_json TEXT,
            event_json TEXT NOT NULL,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE seq_counter (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE team_missions (
            mission_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
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
        CREATE TABLE session_branches (
            child_session_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            branch_from_seq INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def _wired_stack():
    conn = _make_conn()
    session_repo = SessionRepoImpl(conn)
    run_repo = RunRepoImpl(conn)
    mission_repo = TeamMissionRepoImpl(conn)
    mission_orch = TeamMissionOrchestrator(mission_repo)

    registry = MethodRegistry()
    session_methods.register(registry, session_repo)
    run_methods.register(registry, run_repo)
    team_mission_methods.register(registry, mission_orch)

    return {
        "conn": conn,
        "session_repo": session_repo,
        "run_repo": run_repo,
        "mission_repo": mission_repo,
        "mission_orch": mission_orch,
        "registry": registry,
    }


def test_session_get_returns_shape_of_persisted_row():
    stack = _wired_stack()
    stack["session_repo"].create(
        SessionSpec(session_id="s-1", source="test", title="Hello")
    )
    resp = dispatch(
        stack["registry"],
        {"id": "r1", "method": "session.get", "params": {"session_id": "s-1"}},
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert result["session_id"] == "s-1"
    assert result["source"] == "test"
    assert result["title"] == "Hello"


def test_session_get_returns_5005_when_missing():
    stack = _wired_stack()
    resp = dispatch(
        stack["registry"],
        {"id": "r1", "method": "session.get", "params": {"session_id": "no-such"}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value


def test_session_get_normalises_stored_session_id_alias():
    """Phase G identity fold — legacy alias is accepted at the boundary."""
    stack = _wired_stack()
    stack["session_repo"].create(SessionSpec(session_id="s-1", source="test"))
    resp = dispatch(
        stack["registry"],
        {"id": "r1", "method": "session.get", "params": {"storedSessionId": "s-1"}},
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["session_id"] == "s-1"


def test_run_list_events_returns_appended_canonical_events():
    stack = _wired_stack()
    stack["session_repo"].create(SessionSpec(session_id="s-1", source="test"))
    stack["run_repo"].create_run(
        "s-1", RunSpec(run_id="r-1", session_id="s-1")
    )
    stack["run_repo"].append_event(
        "s-1",
        CanonicalEventSpec(
            event_type="message.start", payload={"role": "assistant"}, run_id="r-1"
        ),
    )
    resp = dispatch(
        stack["registry"],
        {
            "id": "req",
            "method": "run.list_events",
            "params": {"session_id": "s-1"},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    events = resp["result"]["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "message.start"
    assert events[0]["seq"] == 1


def test_run_list_events_default_hides_internal_events():
    stack = _wired_stack()
    stack["session_repo"].create(SessionSpec(session_id="s-1", source="test"))
    stack["run_repo"].create_run("s-1", RunSpec(run_id="r-1", session_id="s-1"))
    stack["run_repo"].append_event(
        "s-1",
        CanonicalEventSpec(event_type="message.start", payload={}, run_id="r-1"),
    )
    stack["run_repo"].append_event(
        "s-1",
        CanonicalEventSpec(
            event_type="_internal.interaction.requested",
            payload={"request_id": "req-1"},
            run_id="r-1",
        ),
    )
    resp = dispatch(
        stack["registry"],
        {"id": "req", "method": "run.list_events", "params": {"session_id": "s-1"}},
        resolver=AllowAllResolver(),
    )
    types = [e["event_type"] for e in resp["result"]["events"]]
    assert types == ["message.start"]

    resp_all = dispatch(
        stack["registry"],
        {
            "id": "req",
            "method": "run.list_events",
            "params": {"session_id": "s-1", "include_internal": True},
        },
        resolver=AllowAllResolver(),
    )
    types_all = [e["event_type"] for e in resp_all["result"]["events"]]
    assert types_all == ["message.start", "_internal.interaction.requested"]


def test_team_mission_ready_nodes_and_advance_lifecycle():
    stack = _wired_stack()
    stack["session_repo"].create(SessionSpec(session_id="s-1", source="test"))
    stack["mission_repo"].create_mission(
        "s-1", MissionSpec(mission_id="m-1", session_id="s-1")
    )
    stack["mission_repo"].add_node(NodeSpec(mission_id="m-1", node_id="a"))
    stack["mission_repo"].add_node(NodeSpec(mission_id="m-1", node_id="b"))
    stack["mission_repo"].add_edge(
        EdgeSpec(mission_id="m-1", from_node_id="a", to_node_id="b")
    )

    resp = dispatch(
        stack["registry"],
        {
            "id": "req",
            "method": "team_mission.ready_nodes",
            "params": {"missionId": "m-1"},
        },
        resolver=AllowAllResolver(),
    )
    assert [n["node_id"] for n in resp["result"]["nodes"]] == ["a"]

    resp_adv = dispatch(
        stack["registry"],
        {
            "id": "req",
            "method": "team_mission.advance_node",
            "params": {
                "missionId": "m-1",
                "nodeId": "a",
                "terminalStatus": "completed",
                "sessionId": "s-1",
            },
        },
        resolver=AllowAllResolver(),
    )
    result = resp_adv["result"]
    assert result["newly_ready_nodes"] == ["b"]
    assert len(result["activity_ids_recorded"]) == 1


def test_team_mission_advance_node_rejects_invalid_terminal_status():
    stack = _wired_stack()
    stack["session_repo"].create(SessionSpec(session_id="s-1", source="test"))
    stack["mission_repo"].create_mission(
        "s-1", MissionSpec(mission_id="m-1", session_id="s-1")
    )
    stack["mission_repo"].add_node(NodeSpec(mission_id="m-1", node_id="a"))
    resp = dispatch(
        stack["registry"],
        {
            "id": "req",
            "method": "team_mission.advance_node",
            "params": {
                "missionId": "m-1",
                "nodeId": "a",
                "terminalStatus": "still-running",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_permission_denied_returns_4003_for_write_method():
    stack = _wired_stack()

    class _DenyWrites:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        stack["registry"],
        {
            "id": "req",
            "method": "team_mission.advance_node",
            "params": {
                "missionId": "m-1",
                "nodeId": "a",
                "terminalStatus": "completed",
            },
        },
        resolver=_DenyWrites(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


def test_missing_required_params_returns_4002():
    stack = _wired_stack()
    for method in ("session.get", "run.list_events", "team_mission.ready_nodes"):
        resp = dispatch(
            stack["registry"],
            {"id": "req", "method": method, "params": {}},
            resolver=AllowAllResolver(),
        )
        assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value, method
