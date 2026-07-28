"""Phase G — ``run.reap_orphans`` gateway method (spec §8.1 crash recovery)."""

from __future__ import annotations

import sqlite3

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods.run_methods import register_lifecycle
from hermes_agent.orchestration import RunOrchestrator, WorkerPool


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            runtime_scope_key TEXT,
            worker_id TEXT NOT NULL DEFAULT '',
            agent_profile_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT,
            execution_session_id TEXT,
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
        """
    )
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    conn.commit()
    return conn


def _wired():
    conn = _make_conn()
    orch = RunOrchestrator(WorkerPool())
    registry = MethodRegistry()

    def provider(session_id):
        return conn

    register_lifecycle(registry, orch, provider)
    return conn, orch, registry


def test_reap_orphans_recovers_active_runs_and_returns_pool_size():
    conn, orch, registry = _wired()
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
        "VALUES ('r1', 's1', 'running', 0, 0), "
        "('r2', 's1', 'queued', 0, 0), "
        "('r3', 's1', 'completed', 0, 0)"
    )
    conn.commit()

    resp = dispatch(
        registry,
        {"id": "req", "method": "run.reap_orphans", "params": {"sessionId": "s1"}},
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    result = resp["result"]
    assert set(result["recovered_run_ids"]) == {"r1", "r2"}
    assert result["pool_size"] == 2


def test_reap_orphans_no_active_runs_returns_empty_list():
    conn, orch, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.reap_orphans", "params": {"sessionId": "s1"}},
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["recovered_run_ids"] == []
    assert resp["result"]["pool_size"] == 0


def test_reap_orphans_rejects_missing_session_id():
    conn, orch, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.reap_orphans", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_reap_orphans_write_permission_denied_when_read_only():
    conn, orch, registry = _wired()

    class _RO:
        def is_allowed(self, ctx, permission_name, *, read_only):
            return read_only

    resp = dispatch(
        registry,
        {"id": "req", "method": "run.reap_orphans", "params": {"sessionId": "s1"}},
        resolver=_RO(),
    )
    assert resp["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


def test_reap_orphans_ignores_already_terminal_rows():
    conn, orch, registry = _wired()
    for status in ("completed", "failed", "cancelled", "interrupted"):
        conn.execute(
            "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
            "VALUES (?, 's1', ?, 0, 0)",
            (f"term-{status}", status),
        )
    conn.commit()
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.reap_orphans", "params": {"sessionId": "s1"}},
        resolver=AllowAllResolver(),
    )
    assert resp["result"]["recovered_run_ids"] == []
    assert resp["result"]["pool_size"] == 0
