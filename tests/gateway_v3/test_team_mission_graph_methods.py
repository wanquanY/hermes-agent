"""Phase G — ``team_mission.*`` graph CRUD methods E2E."""

from __future__ import annotations

import sqlite3

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods.team_mission_methods import register_graph
from hermes_agent.repositories import (
    EdgeSpec,
    MissionSpec,
    NodeSpec,
    TeamMissionRepoImpl,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
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
        """
    )
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    conn.commit()
    return conn


def _wired():
    conn = _make_conn()
    repo = TeamMissionRepoImpl(conn)
    registry = MethodRegistry()
    register_graph(registry, repo)
    return conn, repo, registry


def test_team_mission_create_persists_mission():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.create",
            "params": {
                "missionId": "m1",
                "sessionId": "s1",
                "title": "My Mission",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert result["mission_id"] == "m1"
    assert result["title"] == "My Mission"

    row = conn.execute(
        "SELECT title FROM team_missions WHERE mission_id='m1'"
    ).fetchone()
    assert row["title"] == "My Mission"


def test_team_mission_create_rejects_missing_ids():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.create",
            "params": {"missionId": "m1"},  # missing session
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_team_mission_add_node_and_get_node():
    conn, repo, registry = _wired()
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.add_node",
            "params": {
                "missionId": "m1",
                "nodeId": "n1",
                "title": "First Node",
            },
        },
        resolver=AllowAllResolver(),
    )
    got = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.get_node",
            "params": {"missionId": "m1", "nodeId": "n1"},
        },
        resolver=AllowAllResolver(),
    )
    assert got["result"]["node_id"] == "n1"
    assert got["result"]["title"] == "First Node"
    assert got["result"]["status"] == "pending"


def test_team_mission_get_node_missing_returns_not_found():
    conn, repo, registry = _wired()
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.get_node",
            "params": {"missionId": "m1", "nodeId": "missing"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value


def test_team_mission_add_edge_persists():
    conn, repo, registry = _wired()
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    repo.add_node(NodeSpec(mission_id="m1", node_id="a"))
    repo.add_node(NodeSpec(mission_id="m1", node_id="b"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.add_edge",
            "params": {
                "missionId": "m1",
                "fromNodeId": "a",
                "toNodeId": "b",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["from_node_id"] == "a"
    assert resp["result"]["to_node_id"] == "b"
    assert resp["result"]["kind"] == "sequence"


def test_team_mission_get_graph_returns_full_dag():
    conn, repo, registry = _wired()
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    for nid in ("a", "b", "c"):
        repo.add_node(NodeSpec(mission_id="m1", node_id=nid))
    for a, b in (("a", "b"), ("b", "c")):
        repo.add_edge(EdgeSpec(mission_id="m1", from_node_id=a, to_node_id=b))

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.get_graph",
            "params": {"missionId": "m1"},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert [n["node_id"] for n in result["nodes"]] == ["a", "b", "c"]
    assert [(e["from_node_id"], e["to_node_id"]) for e in result["edges"]] == [
        ("a", "b"),
        ("b", "c"),
    ]


def test_team_mission_get_graph_missing_returns_not_found():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.get_graph",
            "params": {"missionId": "nope"},
        },
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value


def test_team_mission_bind_run_persists_binding():
    conn, repo, registry = _wired()
    repo.create_mission("s1", MissionSpec(mission_id="m1", session_id="s1"))
    repo.add_node(NodeSpec(mission_id="m1", node_id="a"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.bind_run",
            "params": {
                "missionId": "m1",
                "nodeId": "a",
                "runId": "run-1",
            },
        },
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["run_id"] == "run-1"
    node = repo.get_node("m1", "a")
    assert node.bound_run_id == "run-1"


def test_team_mission_write_permission_denied_when_read_only():
    conn, repo, registry = _wired()

    class _RO:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "team_mission.create",
            "params": {"missionId": "m1", "sessionId": "s1"},
        },
        resolver=_RO(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


def test_team_mission_full_lifecycle_via_gateway():
    """Build a mission entirely through dispatch, no direct repo calls."""
    conn, repo, registry = _wired()
    for req in [
        {
            "method": "team_mission.create",
            "params": {"missionId": "m1", "sessionId": "s1", "title": "Full flow"},
        },
        {
            "method": "team_mission.add_node",
            "params": {"missionId": "m1", "nodeId": "n1", "title": "Plan"},
        },
        {
            "method": "team_mission.add_node",
            "params": {"missionId": "m1", "nodeId": "n2", "title": "Execute"},
        },
        {
            "method": "team_mission.add_edge",
            "params": {"missionId": "m1", "fromNodeId": "n1", "toNodeId": "n2"},
        },
        {
            "method": "team_mission.bind_run",
            "params": {"missionId": "m1", "nodeId": "n1", "runId": "run-1"},
        },
    ]:
        resp = dispatch(
            registry, {"id": "r", **req}, resolver=AllowAllResolver()
        )
        assert "error" not in resp, req

    graph = dispatch(
        registry,
        {"id": "r", "method": "team_mission.get_graph", "params": {"missionId": "m1"}},
        resolver=AllowAllResolver(),
    )["result"]
    assert graph["mission"]["title"] == "Full flow"
    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 1
