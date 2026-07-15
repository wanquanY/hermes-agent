"""Full-stack daemon test — JSONL in / JSONL out end-to-end.

Proves that ``StdioTransport → TransportRouter → MethodRegistry → dispatch →
Repo → SQLite`` is wired correctly by driving a real SQLite connection with
canned JSONL requests and asserting the JSONL responses.
"""

from __future__ import annotations

import io
import json
import sqlite3

from hermes_agent.transport.stdio_daemon import (
    build_registry_and_router,
    run_daemon,
)


_SCHEMA = """
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
    active_execution_session_id TEXT NOT NULL DEFAULT '',
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


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _write_lines(stream: io.StringIO, wires: list[dict]) -> None:
    for w in wires:
        stream.write(json.dumps(w) + "\n")
    stream.seek(0)


def _read_lines(stream: io.StringIO) -> list[dict]:
    stream.seek(0)
    return [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------


def test_daemon_creates_session_and_returns_projection_via_jsonl():
    conn = _make_conn()
    inbound = io.StringIO()
    outbound = io.StringIO()
    _write_lines(
        inbound,
        [
            {
                "id": "1",
                "method": "session.create",
                "params": {
                    "sessionId": "s1",
                    "source": "stdio-test",
                    "title": "Hello",
                },
            }
        ],
    )

    summary = run_daemon(conn, in_stream=inbound, out_stream=outbound)

    lines = _read_lines(outbound)
    # 1 handshake + 1 response.
    assert len(lines) == 2
    assert lines[0]["type"] == "handshake"
    assert lines[0]["contractVersion"] == "3.1"
    resp = lines[1]
    assert resp["id"] == "1"
    assert resp["type"] == "response"
    assert resp["result"]["session_id"] == "s1"
    assert resp["result"]["title"] == "Hello"

    # SQLite row landed too.
    row = conn.execute("SELECT * FROM sessions WHERE id='s1'").fetchone()
    assert row is not None
    assert row["source"] == "stdio-test"

    assert summary["handshakes_sent"] == 1
    assert summary["requests_dispatched"] == 1
    assert summary["errors_returned"] == 0


def test_daemon_pipelines_multiple_methods_in_one_batch():
    conn = _make_conn()
    inbound = io.StringIO()
    outbound = io.StringIO()
    _write_lines(
        inbound,
        [
            {
                "id": "a",
                "method": "session.create",
                "params": {"sessionId": "s1", "source": "t", "title": "One"},
            },
            {
                "id": "b",
                "method": "session.get",
                "params": {"sessionId": "s1"},
            },
        ],
    )

    summary = run_daemon(conn, in_stream=inbound, out_stream=outbound)

    lines = _read_lines(outbound)
    # 1 handshake + 2 responses.
    assert len(lines) == 3
    assert lines[1]["id"] == "a"
    assert lines[2]["id"] == "b"
    assert lines[2]["result"]["session_id"] == "s1"
    assert lines[2]["result"]["title"] == "One"

    assert summary["requests_dispatched"] == 2
    assert summary["errors_returned"] == 0


def test_daemon_returns_error_envelope_for_unknown_method():
    conn = _make_conn()
    inbound = io.StringIO()
    outbound = io.StringIO()
    _write_lines(
        inbound,
        [{"id": "z", "method": "does.not.exist", "params": {}}],
    )

    summary = run_daemon(conn, in_stream=inbound, out_stream=outbound)

    lines = _read_lines(outbound)
    err = lines[1]
    assert err["type"] == "error"
    assert err["id"] == "z"
    # Method not found (4001 in current error_codes taxonomy).
    assert err["error"]["code"] == "4001"

    assert summary["errors_returned"] == 1


def test_daemon_answers_handshake_frame_on_startup_only():
    conn = _make_conn()
    inbound = io.StringIO()
    outbound = io.StringIO()
    # No input at all - just start + EOF.
    summary = run_daemon(conn, in_stream=inbound, out_stream=outbound)
    lines = _read_lines(outbound)
    assert len(lines) == 1
    assert lines[0]["type"] == "handshake"
    assert summary["frames_processed"] == 0


def test_daemon_survives_malformed_line_and_processes_next():
    conn = _make_conn()
    inbound = io.StringIO()
    outbound = io.StringIO()
    # First line is garbage, second line is a real request.
    inbound.write("this is not json\n")
    inbound.write(json.dumps({
        "id": "ok",
        "method": "session.create",
        "params": {"sessionId": "s1", "source": "t"},
    }) + "\n")
    inbound.seek(0)

    summary = run_daemon(conn, in_stream=inbound, out_stream=outbound)

    lines = _read_lines(outbound)
    # 1 handshake + 1 MALFORMED_FRAME error + 1 real response
    assert len(lines) == 3
    assert lines[0]["type"] == "handshake"
    assert lines[1]["type"] == "error"
    assert lines[1]["error"]["code"] == "4006"
    assert lines[2]["type"] == "response"
    assert lines[2]["id"] == "ok"

    assert summary["requests_dispatched"] == 1
    assert summary["errors_returned"] == 1


def test_daemon_build_registry_registers_all_five_aggregates():
    """The daemon builder must wire every aggregate root — smoke check by
    listing the registered methods.
    """
    conn = _make_conn()
    inbound = io.StringIO()
    outbound = io.StringIO()
    registry, _router = build_registry_and_router(
        conn, in_stream=inbound, out_stream=outbound
    )
    methods = set(registry.names())
    # One representative method from each of the five aggregate roots.
    assert "session.create" in methods
    assert "run.launch" in methods
    assert "message.append" in methods
    assert "team_mission.create" in methods
    assert "agent_profile.list" in methods
    # Plus handshake.
    assert "system.handshake" in methods
