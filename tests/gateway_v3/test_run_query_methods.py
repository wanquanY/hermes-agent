"""Phase G — ``run.get`` + ``run.list`` E2E."""

from __future__ import annotations

import sqlite3

from hermes_agent.gateway import (
    AllowAllResolver,
    ErrorCode,
    MethodRegistry,
    dispatch,
)
from hermes_agent.gateway.methods.run_methods import register
from hermes_agent.repositories import RunRepoImpl, RunSpec


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
        """
    )
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    conn.commit()
    return conn


def _wired():
    conn = _make_conn()
    repo = RunRepoImpl(conn)
    registry = MethodRegistry()

    def provider(session_id):
        assert session_id == "s1"
        return conn

    register(registry, repo, conn_provider=provider)
    return conn, repo, registry


def test_run_get_returns_projection():
    conn, repo, registry = _wired()
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1", turn_id="t1"))
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.get", "params": {"runId": "r1"}},
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["run_id"] == "r1"
    assert resp["result"]["status"] == "running"
    assert resp["result"]["turn_id"] == "t1"


def test_run_get_missing_returns_5004():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.get", "params": {"runId": "missing"}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.RUN_NOT_FOUND.value


def test_run_get_rejects_missing_run_id():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.get", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_run_list_returns_all_runs_under_session():
    conn, repo, registry = _wired()
    for i in range(3):
        repo.create_run(
            "s1", RunSpec(run_id=f"r{i}", session_id="s1", turn_id=f"t{i}")
        )
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.list", "params": {"sessionId": "s1"}},
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    ids = {r["run_id"] for r in resp["result"]["runs"]}
    assert ids == {"r0", "r1", "r2"}


def test_run_list_filters_by_status():
    conn, repo, registry = _wired()
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    repo.create_run("s1", RunSpec(run_id="r2", session_id="s1"))
    conn.execute("UPDATE runs SET status='completed' WHERE run_id='r2'")
    conn.commit()

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.list",
            "params": {"sessionId": "s1", "status": "running"},
        },
        resolver=AllowAllResolver(),
    )
    ids = [r["run_id"] for r in resp["result"]["runs"]]
    assert ids == ["r1"]


def test_run_list_status_can_be_list_of_values():
    conn, repo, registry = _wired()
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    repo.create_run("s1", RunSpec(run_id="r2", session_id="s1"))
    repo.create_run("s1", RunSpec(run_id="r3", session_id="s1"))
    conn.execute("UPDATE runs SET status='completed' WHERE run_id='r2'")
    conn.execute("UPDATE runs SET status='failed' WHERE run_id='r3'")
    conn.commit()

    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.list",
            "params": {"sessionId": "s1", "status": ["completed", "failed"]},
        },
        resolver=AllowAllResolver(),
    )
    ids = {r["run_id"] for r in resp["result"]["runs"]}
    assert ids == {"r2", "r3"}


def test_run_list_rejects_missing_session_id():
    conn, repo, registry = _wired()
    resp = dispatch(
        registry,
        {"id": "req", "method": "run.list", "params": {}},
        resolver=AllowAllResolver(),
    )
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS.value


def test_run_get_identity_fold_at_dispatch_boundary():
    conn, repo, registry = _wired()
    repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
    resp = dispatch(
        registry,
        {
            "id": "req",
            "method": "run.get",
            "params": {"runId": "r1", "storedSessionId": "s1"},
        },
        resolver=AllowAllResolver(),
    )
    assert "error" not in resp
    assert resp["result"]["run_id"] == "r1"
