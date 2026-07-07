"""Full 5-layer integration — Transport → Router → dispatch → orchestrator → repo → SQLite.

Proves the whole v3 stack round-trips a realistic mix of session / run /
message / team_mission / agent_profile traffic against a single SQLite
database, without touching any legacy tui_gateway path.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.gateway import (
    AllowAllResolver,
    MethodRegistry,
)
from hermes_agent.gateway.methods import (
    agent_profile_methods,
    handshake_method,
    message_methods,
    run_methods,
    session_methods,
    team_mission_methods,
)
from hermes_agent.orchestration import (
    RunOrchestrator,
    TeamMissionOrchestrator,
    WorkerPool,
)
from hermes_agent.repositories import (
    AgentProfileRepoImpl,
    MessageRepoImpl,
    RunRepoImpl,
    SessionRepoImpl,
    TeamMissionRepoImpl,
)
from hermes_agent.transport import (
    InMemoryTransport,
    TransportRouter,
)
from hermes_agent.transport.envelope import FrameKind


def _full_stack():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
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
        CREATE TABLE session_branches (
            child_session_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            branch_from_seq INTEGER NOT NULL,
            created_at REAL NOT NULL
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
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            participant_id TEXT NOT NULL DEFAULT '',
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            reasoning TEXT,
            conversation_message_id TEXT NOT NULL DEFAULT '',
            platform_message_id TEXT,
            metadata_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
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
        CREATE TABLE agent_profiles (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            avatar TEXT,
            description TEXT,
            category TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            is_system_default INTEGER NOT NULL DEFAULT 0,
            hermes_profile_name TEXT,
            hermes_home_path TEXT NOT NULL DEFAULT '',
            default_model TEXT,
            current_version_id TEXT NOT NULL DEFAULT '',
            current_version_number INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE agent_profile_versions (
            profile_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            payload_json TEXT,
            created_at REAL NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (profile_id, version_id)
        );
        CREATE TABLE agent_profile_growth_summary (
            profile_id TEXT PRIMARY KEY,
            total_runs INTEGER NOT NULL DEFAULT 0,
            total_messages INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            growth_score REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()

    session_repo = SessionRepoImpl(conn)
    run_repo = RunRepoImpl(conn)
    message_repo = MessageRepoImpl(conn)
    mission_repo = TeamMissionRepoImpl(conn)
    profile_repo = AgentProfileRepoImpl(conn)
    mission_orch = TeamMissionOrchestrator(mission_repo)
    run_orch = RunOrchestrator(WorkerPool())

    def conn_provider(session_id):
        return conn

    registry = MethodRegistry()
    handshake_method.register(registry)
    session_methods.register(registry, session_repo)
    run_methods.register(registry, run_repo, conn_provider=conn_provider)
    from hermes_agent.gateway.methods.run_methods import register_lifecycle
    register_lifecycle(registry, run_orch, conn_provider)
    message_methods.register(registry, message_repo)
    team_mission_methods.register(registry, mission_orch)
    from hermes_agent.gateway.methods.team_mission_methods import register_graph
    register_graph(registry, mission_repo)
    agent_profile_methods.register(registry, profile_repo)

    transport = InMemoryTransport()
    router = TransportRouter(
        transport=transport,
        registry=registry,
        resolver=AllowAllResolver(),
    )
    return conn, transport, router


def _call(transport, router, req):
    """Send a request through the transport, run the pump, and return the
    single outbound frame that came back.
    """
    transport.client_send({"id": req.get("id", "r"), **req})
    router.pump_one()
    frames = transport.pop_sent()
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}"
    return frames[0]


def test_full_stack_greeting_carries_v31_capabilities():
    conn, transport, router = _full_stack()
    router.start()
    handshake = transport.pop_sent()[0]
    wire = handshake.to_wire()
    assert wire["type"] == "handshake"
    assert wire["contractVersion"] == "3.1"
    assert set(wire["capabilities"]["cursor"].keys()) == {
        "afterSeq",
        "afterId",
        "beforeSeq",
        "beforeId",
    }


def test_full_stack_end_to_end_scenario_via_transport():
    """A single session's realistic flow: create → agent profile → team
    mission with 2 nodes → 3 messages → 1 run launch+terminate. Every
    request goes through the transport router; the router talks to no
    legacy code.
    """
    conn, transport, router = _full_stack()
    router.start()
    transport.pop_sent()

    # Session.
    resp = _call(
        transport,
        router,
        {"method": "session.create", "params": {"sessionId": "s1", "source": "test", "title": "E2E"}},
    )
    assert resp.kind is FrameKind.RESPONSE
    assert resp.result["session_id"] == "s1"

    # Profile.
    _call(
        transport,
        router,
        {
            "method": "agent_profile.create",
            "params": {
                "profileId": "p1",
                "slug": "assistant",
                "name": "Assistant",
                "hermesHomePath": "/tmp/p1",
            },
        },
    )

    # Mission graph a → b.
    _call(transport, router, {"method": "team_mission.create", "params": {"missionId": "m1", "sessionId": "s1"}})
    _call(transport, router, {"method": "team_mission.add_node", "params": {"missionId": "m1", "nodeId": "a"}})
    _call(transport, router, {"method": "team_mission.add_node", "params": {"missionId": "m1", "nodeId": "b"}})
    _call(transport, router, {"method": "team_mission.add_edge", "params": {"missionId": "m1", "fromNodeId": "a", "toNodeId": "b"}})

    graph = _call(
        transport, router,
        {"method": "team_mission.get_graph", "params": {"missionId": "m1"}},
    )
    assert len(graph.result["nodes"]) == 2

    # Messages.
    for i, role in enumerate(("user", "assistant", "user")):
        _call(
            transport, router,
            {"method": "message.append", "params": {"sessionId": "s1", "role": role, "content": f"msg-{i}"}},
        )
    page = _call(
        transport, router,
        {"method": "message.get_page", "params": {"sessionId": "s1"}},
    )
    assert len(page.result["messages"]) == 3

    # Run row (create through direct SQL — simulating what real gateway does).
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
        "VALUES ('r1', 's1', 'running', 0, 0)"
    )
    conn.commit()

    # Launch + terminate via transport.
    launched = _call(
        transport, router,
        {"method": "run.launch", "params": {"runId": "r1", "sessionId": "s1", "workerId": "w1"}},
    )
    assert launched.result["start_seq"] == 1

    terminated = _call(
        transport, router,
        {"method": "run.terminate", "params": {"runId": "r1", "sessionId": "s1", "targetStatus": "completed"}},
    )
    assert terminated.result["outcome"] == "applied"

    # Stats reflect all traffic.
    stats = router.stats()
    assert stats.handshakes_sent == 1
    assert stats.errors_returned == 0
    assert stats.requests_dispatched > 0


def test_full_stack_handles_mixed_errors_without_breaking_router():
    """Interleave good and bad requests — router keeps processing."""
    conn, transport, router = _full_stack()
    router.start()
    transport.pop_sent()

    # bad method
    err_frame = _call(
        transport, router,
        {"method": "no.such", "params": {}},
    )
    assert err_frame.kind is FrameKind.ERROR
    assert err_frame.error["code"] == "4001"

    # Missing required param → INVALID_PARAMS
    err_frame2 = _call(
        transport, router,
        {"method": "session.get", "params": {}},
    )
    assert err_frame2.kind is FrameKind.ERROR
    assert err_frame2.error["code"] == "4002"

    # Router recovers and serves next request fine
    good = _call(
        transport, router,
        {"method": "system.handshake", "params": {}},
    )
    assert good.kind is FrameKind.RESPONSE
    assert good.result["contractVersion"] == "3.1"

    stats = router.stats()
    assert stats.errors_returned >= 2
    assert stats.requests_dispatched >= 1
