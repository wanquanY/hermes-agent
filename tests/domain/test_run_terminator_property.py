"""Phase K — property-style tests for RunStateMachine.terminate_run (spec §J4)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.domain.run_terminator import (
    TerminateCause,
    TerminateOutcome,
    terminate_run,
)


PROPERTY_ROUNDS = 24


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
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
    return conn


_TERMINAL_TARGETS = ("completed", "failed", "interrupted", "cancelled")
_CAUSES = list(TerminateCause)


def _init_run(conn: sqlite3.Connection, session_id: str, run_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,))
    conn.execute(
        "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
        "VALUES (?, ?, 'running', 0, 0)",
        (run_id, session_id),
    )
    conn.commit()


def test_property_terminate_is_idempotent_under_many_repeats():
    """N random repeats produce exactly ONE canonical terminal event."""
    rng = random.Random(20260714)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        _init_run(conn, "s1", "r1")
        repeats = rng.randint(1, 25)
        targets = [rng.choice(_TERMINAL_TARGETS) for _ in range(repeats)]
        causes = [rng.choice(_CAUSES) for _ in range(repeats)]

        outcomes = []
        for target, cause in zip(targets, causes):
            result = terminate_run(
                conn,
                run_id="r1",
                session_id="s1",
                target_status=target,
                cause=cause,
            )
            outcomes.append(result.outcome)

        applied_count = sum(1 for o in outcomes if o is TerminateOutcome.APPLIED)
        skip_count = sum(1 for o in outcomes if o is TerminateOutcome.IDEMPOTENT_SKIP)
        assert applied_count == 1, (
            f"round {round_idx}: exactly one APPLIED expected, got {applied_count}"
        )
        assert applied_count + skip_count == repeats

        event_count = conn.execute(
            "SELECT COUNT(*) AS n FROM run_events WHERE session_id='s1'"
        ).fetchone()["n"]
        assert event_count == 1, (
            f"round {round_idx}: exactly one canonical terminal event expected, got {event_count}"
        )
        conn.close()


def test_property_terminal_never_downgrades():
    """Whatever target the first APPLIED landed, later calls must not change status."""
    rng = random.Random(20260715)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        _init_run(conn, "s1", "r1")

        first_target = rng.choice(_TERMINAL_TARGETS)
        first_cause = rng.choice(_CAUSES)
        result = terminate_run(
            conn,
            run_id="r1",
            session_id="s1",
            target_status=first_target,
            cause=first_cause,
        )
        assert result.outcome is TerminateOutcome.APPLIED

        for _ in range(rng.randint(2, 15)):
            new_target = rng.choice(_TERMINAL_TARGETS)
            follow_up = terminate_run(
                conn,
                run_id="r1",
                session_id="s1",
                target_status=new_target,
                cause=rng.choice(_CAUSES),
            )
            assert follow_up.outcome is TerminateOutcome.IDEMPOTENT_SKIP
            assert follow_up.terminal_status == first_target, (
                f"round {round_idx}: terminal must be absorbing"
            )

        row = conn.execute("SELECT status FROM runs WHERE run_id='r1'").fetchone()
        assert row["status"] == first_target


def test_property_independent_runs_never_cross_contaminate():
    """N runs terminating concurrently keep independent terminal_seq."""
    rng = random.Random(20260716)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        run_count = rng.randint(2, 8)
        run_ids = [f"r-{i}" for i in range(run_count)]
        for run_id in run_ids:
            _init_run(conn, "s1", run_id)

        results = []
        for run_id in run_ids:
            result = terminate_run(
                conn,
                run_id=run_id,
                session_id="s1",
                target_status=rng.choice(_TERMINAL_TARGETS),
                cause=rng.choice(_CAUSES),
            )
            assert result.outcome is TerminateOutcome.APPLIED
            results.append(result.terminal_seq)

        # All terminal_seq unique and strictly monotonic per session.
        assert results == sorted(results)
        assert len(set(results)) == len(results)

        # Every run row reflects its own terminal_seq (no leak across runs).
        for run_id, expected_seq in zip(run_ids, results):
            row = conn.execute(
                "SELECT terminal_seq FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert row["terminal_seq"] == expected_seq

        conn.close()
