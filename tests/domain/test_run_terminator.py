"""Phase C — RunStateMachine.terminate_run atomic transition (spec §7.2)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_agent.domain.exceptions import SeqAllocatorBusy
from hermes_agent.domain.run_terminator import (
    TerminateCause,
    TerminateOutcome,
    terminate_run,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
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
            activity_id TEXT,
            timestamp REAL NOT NULL,
            payload_json TEXT,
            event_json TEXT NOT NULL,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE seq_counter (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, created_at) VALUES (?, 'test', 0)",
        ("sess-1",),
    )
    conn.execute(
        """
        INSERT INTO runs (run_id, session_id, status, started_at, updated_at)
        VALUES ('run-1', 'sess-1', 'running', 0, 0)
        """
    )
    conn.commit()
    return conn


def test_terminate_run_applied_writes_event_and_status():
    conn = _make_conn()
    now = time.time()

    result = terminate_run(
        conn,
        run_id="run-1",
        session_id="sess-1",
        target_status="completed",
        cause=TerminateCause.WORKER_EMITTED,
        turn_id="turn-a",
        now=now,
    )

    assert result.outcome is TerminateOutcome.APPLIED
    assert result.terminal_status == "completed"
    assert result.terminal_seq == 1
    assert result.degraded is False

    run_row = conn.execute(
        "SELECT status, terminal_seq, terminal_cause, terminal_degraded FROM runs WHERE run_id = 'run-1'"
    ).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["terminal_seq"] == 1
    assert run_row["terminal_cause"] == "worker_emitted"
    assert run_row["terminal_degraded"] == 0

    event_row = conn.execute(
        "SELECT event_type, seq, payload_json FROM run_events WHERE session_id = 'sess-1'"
    ).fetchone()
    assert event_row["event_type"] == "message.complete"
    assert event_row["seq"] == 1
    payload = json.loads(event_row["payload_json"])
    assert payload["status"] == "completed"
    assert payload["run_id"] == "run-1"


def test_terminate_run_idempotent_skip_after_first_terminal():
    conn = _make_conn()

    first = terminate_run(
        conn,
        run_id="run-1",
        session_id="sess-1",
        target_status="completed",
        cause=TerminateCause.WORKER_EMITTED,
    )
    assert first.outcome is TerminateOutcome.APPLIED

    second = terminate_run(
        conn,
        run_id="run-1",
        session_id="sess-1",
        target_status="failed",  # attempt to downgrade
        cause=TerminateCause.WORKER_CRASHED,
    )

    assert second.outcome is TerminateOutcome.IDEMPOTENT_SKIP
    assert second.terminal_status == "completed"  # not downgraded
    assert second.terminal_seq == 1

    event_count = conn.execute(
        "SELECT COUNT(*) AS n FROM run_events WHERE session_id = 'sess-1'"
    ).fetchone()["n"]
    assert event_count == 1, "second terminate must not append a new event"


def test_terminate_run_error_status_maps_to_error_event():
    conn = _make_conn()

    result = terminate_run(
        conn,
        run_id="run-1",
        session_id="sess-1",
        target_status="failed",
        cause=TerminateCause.WORKER_CRASHED,
        message="oom-killed",
    )
    assert result.outcome is TerminateOutcome.APPLIED

    event_row = conn.execute(
        "SELECT event_type, payload_json FROM run_events WHERE session_id = 'sess-1'"
    ).fetchone()
    assert event_row["event_type"] == "error"
    payload = json.loads(event_row["payload_json"])
    assert payload["status"] == "failed"
    assert payload["message"] == "oom-killed"
    assert payload["error_code"] == "runtime_error"


def test_terminate_run_stamps_activity_id_on_terminal_event():
    conn = _make_conn()

    terminate_run(
        conn,
        run_id="run-1",
        session_id="sess-1",
        target_status="failed",
        cause=TerminateCause.WORKER_CRASHED,
        activity_id="mission:mission-1",
        payload_extra={"activity_id": "mission:mission-1"},
    )

    row = conn.execute("SELECT activity_id, payload_json FROM run_events").fetchone()
    assert row["activity_id"] == "mission:mission-1"
    payload = json.loads(row["payload_json"])
    assert payload["activity_id"] == "mission:mission-1"


def test_terminate_run_rejects_non_terminal_target():
    conn = _make_conn()
    with pytest.raises(ValueError):
        terminate_run(
            conn,
            run_id="run-1",
            session_id="sess-1",
            target_status="running",
            cause=TerminateCause.WORKER_EMITTED,
        )


def test_terminate_run_degraded_when_seq_allocator_busy(monkeypatch):
    conn = _make_conn()

    from hermes_agent.domain import run_terminator as terminator_module

    def _busy(*_args, **_kwargs):
        raise SeqAllocatorBusy("seq_counter locked (simulated)")

    monkeypatch.setattr(terminator_module, "_allocate_terminal_seq", _busy)

    result = terminate_run(
        conn,
        run_id="run-1",
        session_id="sess-1",
        target_status="failed",
        cause=TerminateCause.WORKER_CRASHED,
        message="degraded-path",
    )

    assert result.outcome is TerminateOutcome.DEGRADED
    assert result.terminal_status == "failed"
    assert result.terminal_seq == 0
    assert result.degraded is True

    run_row = conn.execute(
        "SELECT status, terminal_seq, terminal_degraded, error FROM runs WHERE run_id = 'run-1'"
    ).fetchone()
    assert run_row["status"] == "failed"
    assert run_row["terminal_seq"] == 0
    assert run_row["terminal_degraded"] == 1
    assert run_row["error"] == "degraded-path"

    event_count = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE session_id = 'sess-1'"
    ).fetchone()[0]
    assert event_count == 0, "degrade path must NOT append a canonical terminal event"


def test_terminate_run_requires_run_id_and_session():
    conn = _make_conn()
    with pytest.raises(ValueError):
        terminate_run(
            conn,
            run_id="",
            session_id="sess-1",
            target_status="completed",
            cause=TerminateCause.WORKER_EMITTED,
        )
