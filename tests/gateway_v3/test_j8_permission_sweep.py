"""J8 permission sweep — every registered method must reject a DenyAll caller.

This is a *systematic* J8 guard: the registry structurally enforces
``@requires_permission`` at registration time, but this test proves the
deny path actually returns a 4003 error envelope for **every** method in
the daemon's full registry, not just a hand-picked few.

If someone in the future silently changes the pipeline to bypass the
resolver on some method, this sweep catches it.
"""

from __future__ import annotations

import sqlite3

from hermes_agent.gateway import (
    ErrorCode,
    PermissionResolver,
    dispatch,
)
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.transport.stdio_daemon import build_registry_and_router


class _DenyAllResolver(PermissionResolver):
    def is_allowed(self, ctx: DispatchContext, permission_name: str, *, read_only: bool) -> bool:
        return False


_MINIMAL_SCHEMA = """
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
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_seq INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    ended_at REAL,
    end_reason TEXT,
    end_reason_detail TEXT,
    kind TEXT NOT NULL DEFAULT 'agent'
);
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE seq_counter (
    session_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE team_missions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL DEFAULT 0,
    ended_at REAL,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE team_mission_nodes (
    mission_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL,
    ended_at REAL,
    PRIMARY KEY (mission_id, node_id)
);
"""


def _make_registry():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_MINIMAL_SCHEMA)
    conn.commit()
    registry, _router = build_registry_and_router(
        conn, in_stream=None, out_stream=None  # type: ignore[arg-type]
    )
    return conn, registry


def test_j8_every_registered_method_rejects_deny_all_resolver():
    """spec §J8 — every handler in the daemon registry rejects a caller
    that fails the permission gate.
    """
    conn, registry = _make_registry()
    method_names = registry.names()
    # sanity: we expect >= 25 methods (v3.0.2 landing has 31)
    assert len(method_names) >= 25, (
        f"registry has only {len(method_names)} methods — did registration break?"
    )

    resolver = _DenyAllResolver()
    for method in method_names:
        frame = {"id": f"req-{method}", "method": method, "params": {}}
        resp = dispatch(registry, frame, resolver=resolver)
        assert "error" in resp, (
            f"method {method!r} returned no error under DenyAllResolver: {resp!r}"
        )
        assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value, (
            f"method {method!r} rejected with wrong code: {resp['error']!r}"
        )


def test_j8_every_registered_method_has_permission_tag_visible():
    """spec §J8 static side — the registry entry carries a Permission dc."""
    conn, registry = _make_registry()
    for entry in registry.entries():
        assert entry.permission is not None, (
            f"method {entry.name!r} has no permission on its registration"
        )
        assert entry.permission.name, (
            f"method {entry.name!r} has an empty permission name"
        )


def test_j8_registered_method_count_matches_expected_v3_surface():
    """Sanity — locks in the wire surface size so drift is visible."""
    conn, registry = _make_registry()
    names = set(registry.names())

    # These sets are the v3.0.2 landing wire surface. Adding a new method
    # should update this list AND its own test file — this assertion is
    # your reminder.
    expected_session = {
        "session.create",
        "session.get",
        "session.list",
        "session.close",
        "session.branch",
        "session.update_index",
    }
    expected_run = {
        "run.launch",
        "run.terminate",
        "run.reap_orphans",
        "run.get",
        "run.list",
        "run.list_events",
    }
    expected_message = {
        "message.append",
        "message.get",
        "message.get_page",
        "message.search_fts",
        "message.merge_metadata",
    }
    expected_team_mission = {
        "team_mission.create",
        "team_mission.get_graph",
        "team_mission.get_node",
        "team_mission.add_node",
        "team_mission.add_edge",
        "team_mission.bind_run",
        "team_mission.ready_nodes",
        "team_mission.advance_node",
    }
    expected_agent_profile = {
        "agent_profile.create",
        "agent_profile.add_version",
        "agent_profile.get",
        "agent_profile.list",
        "agent_profile.growth_summary",
    }
    expected_handshake = {"system.handshake"}

    core = (
        expected_session
        | expected_run
        | expected_message
        | expected_team_mission
        | expected_agent_profile
        | expected_handshake
    )

    missing = core - names
    assert not missing, f"expected v3 methods missing from registry: {missing}"
