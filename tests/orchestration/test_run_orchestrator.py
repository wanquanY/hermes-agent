"""Phase F — RunOrchestrator end-to-end (spec §3, §8)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_terminator import TerminateCause, TerminateOutcome
from hermes_agent.orchestration import (
    RunLaunchResult,
    RunLaunchSpec,
    RunOrchestrator,
    WorkerPool,
)


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
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
        "VALUES ('r1', 's1', 'running', 0, 0)"
    )
    conn.commit()
    return conn


def test_launch_records_inflight_and_appends_run_started_event():
    conn = _make_conn()
    orch = RunOrchestrator(WorkerPool())
    result = orch.launch(
        conn,
        RunLaunchSpec(run_id="r1", session_id="s1", worker_id="w1", turn_id="t1"),
    )
    assert isinstance(result, RunLaunchResult)
    assert result.start_seq == 1

    inflight = orch.pool.get("r1")
    assert inflight is not None
    assert inflight.worker_id == "w1"
    assert inflight.turn_id == "t1"

    row = conn.execute(
        "SELECT event_type, seq FROM run_events WHERE session_id='s1'"
    ).fetchone()
    assert row["event_type"] == "run.started"
    assert row["seq"] == 1


def test_launch_rejects_missing_ids():
    orch = RunOrchestrator(WorkerPool())
    conn = _make_conn()
    with pytest.raises(ValueError):
        orch.launch(
            conn,
            RunLaunchSpec(run_id="", session_id="s1", worker_id="w1"),
        )
    with pytest.raises(ValueError):
        orch.launch(
            conn,
            RunLaunchSpec(run_id="r1", session_id="", worker_id="w1"),
        )
    with pytest.raises(ValueError):
        orch.launch(
            conn,
            RunLaunchSpec(run_id="r1", session_id="s1", worker_id=""),
        )


def test_terminate_clears_inflight_and_delegates_to_domain():
    conn = _make_conn()
    orch = RunOrchestrator(WorkerPool())
    orch.launch(conn, RunLaunchSpec(run_id="r1", session_id="s1", worker_id="w1"))
    assert orch.pool.size() == 1

    result = orch.terminate(
        conn,
        run_id="r1",
        session_id="s1",
        target_status="completed",
        cause=TerminateCause.WORKER_EMITTED,
    )
    assert result.outcome is TerminateOutcome.APPLIED
    assert orch.pool.size() == 0

    row = conn.execute(
        "SELECT status, terminal_seq FROM runs WHERE run_id='r1'"
    ).fetchone()
    assert row["status"] == "completed"
    assert row["terminal_seq"] > 0


def test_terminate_idempotent_skip_does_not_double_clear():
    conn = _make_conn()
    orch = RunOrchestrator(WorkerPool())
    orch.launch(conn, RunLaunchSpec(run_id="r1", session_id="s1", worker_id="w1"))
    orch.terminate(
        conn,
        run_id="r1",
        session_id="s1",
        target_status="completed",
    )
    assert orch.pool.size() == 0

    # Re-launch pretends a caller re-added the run somehow; the second
    # terminate should be idempotent-skip and MUST NOT remove the entry.
    orch.pool.record_run_start(
        worker_id="w1", run_id="r1", session_id="s1"
    )
    result = orch.terminate(
        conn,
        run_id="r1",
        session_id="s1",
        target_status="failed",
    )
    assert result.outcome is TerminateOutcome.IDEMPOTENT_SKIP
    # Idempotent-skip does not touch pool — the re-added entry stays.
    assert orch.pool.size() == 1


def test_reap_orphans_rebuilds_pool_from_active_runs_table():
    conn = _make_conn()
    # Seed a second orphan active run directly in the runs table.
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at, "
        "runtime_scope_key, turn_id) "
        "VALUES ('r2', 's1', 'running', 0, 0, 'w2', 't2')"
    )
    conn.commit()

    orch = RunOrchestrator(WorkerPool())
    assert orch.pool.size() == 0
    recovered = orch.reap_orphans(conn)
    assert set(recovered) == {"r1", "r2"}
    assert orch.pool.size() == 2
    assert orch.pool.get("r2").worker_id == "w2"


def test_reap_orphans_ignores_terminal_runs():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
        "VALUES ('r-done', 's1', 'completed', 0, 0)"
    )
    conn.commit()

    orch = RunOrchestrator(WorkerPool())
    recovered = orch.reap_orphans(conn)
    # r1 was seeded as 'running', so it should be picked up. r-done is terminal.
    assert recovered == ["r1"]
    assert orch.pool.size() == 1


def test_launch_seq_shared_with_subsequent_ledger_appends():
    """RunOrchestrator.launch uses the same SeqAllocator as raw EventLedger writes."""
    conn = _make_conn()
    orch = RunOrchestrator(WorkerPool())
    orch.launch(conn, RunLaunchSpec(run_id="r1", session_id="s1", worker_id="w1"))
    ledger = EventLedger(conn)
    outcome = ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="message.start",
        payload={"role": "assistant"},
    )
    assert outcome.seq == 2  # launch consumed seq=1
