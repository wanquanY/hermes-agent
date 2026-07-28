"""Phase K — property-style tests for RunOrchestrator (spec §3, §8.1)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.domain.run_terminator import TerminateCause, TerminateOutcome
from hermes_agent.orchestration import (
    RunLaunchSpec,
    RunOrchestrator,
    WorkerPool,
)


PROPERTY_ROUNDS = 24


def _make_conn(session_id: str = "s1") -> sqlite3.Connection:
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
            activity_id TEXT,
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
    conn.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
    conn.commit()
    return conn


def _seed_runs(conn: sqlite3.Connection, run_ids: list[str]) -> None:
    for run_id in run_ids:
        conn.execute(
            "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
            "VALUES (?, 's1', 'running', 0, 0)",
            (run_id,),
        )
    conn.commit()


_TERMINAL_TARGETS = ("completed", "failed", "interrupted", "cancelled")
_CAUSES = list(TerminateCause)


def test_property_pool_size_matches_launched_minus_terminated():
    """After every launch/terminate mix, pool.size() equals live-inflight count."""
    rng = random.Random(20260707)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        orch = RunOrchestrator(WorkerPool())
        n_runs = rng.randint(1, 15)
        run_ids = [f"r-{round_idx}-{i}" for i in range(n_runs)]
        _seed_runs(conn, run_ids)

        launched: set[str] = set()
        terminated: set[str] = set()

        # Random sequence of launches and terminations.
        for _ in range(rng.randint(n_runs, n_runs * 3)):
            action = rng.choice(("launch", "terminate"))
            if action == "launch":
                candidates = [r for r in run_ids if r not in launched]
                if not candidates:
                    continue
                run_id = rng.choice(candidates)
                orch.launch(
                    conn,
                    RunLaunchSpec(
                        run_id=run_id,
                        session_id="s1",
                        worker_id=f"w-{rng.randint(1, 5)}",
                    ),
                )
                launched.add(run_id)
            else:
                candidates = [r for r in launched if r not in terminated]
                if not candidates:
                    continue
                run_id = rng.choice(candidates)
                orch.terminate(
                    conn,
                    run_id=run_id,
                    session_id="s1",
                    target_status=rng.choice(_TERMINAL_TARGETS),
                    cause=rng.choice(_CAUSES),
                )
                terminated.add(run_id)

        expected_alive = len(launched - terminated)
        assert orch.pool.size() == expected_alive, (
            f"round {round_idx}: pool.size mismatch, expected {expected_alive}, "
            f"got {orch.pool.size()}"
        )
        conn.close()


def test_property_double_terminate_is_idempotent():
    """K random re-terminations after the first APPLIED all IDEMPOTENT_SKIP,
    and the pool never grows past zero for the affected run.
    """
    rng = random.Random(20260708)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        orch = RunOrchestrator(WorkerPool())
        _seed_runs(conn, ["r1"])
        orch.launch(conn, RunLaunchSpec(run_id="r1", session_id="s1", worker_id="w1"))

        first = orch.terminate(
            conn,
            run_id="r1",
            session_id="s1",
            target_status=rng.choice(_TERMINAL_TARGETS),
            cause=rng.choice(_CAUSES),
        )
        assert first.outcome is TerminateOutcome.APPLIED
        assert orch.pool.size() == 0

        k = rng.randint(2, 15)
        for _ in range(k):
            follow = orch.terminate(
                conn,
                run_id="r1",
                session_id="s1",
                target_status=rng.choice(_TERMINAL_TARGETS),
                cause=rng.choice(_CAUSES),
            )
            assert follow.outcome is TerminateOutcome.IDEMPOTENT_SKIP
            assert follow.terminal_status == first.terminal_status
            assert orch.pool.size() == 0
        conn.close()


def test_property_terminal_seqs_are_disjoint_across_runs():
    """Every completed run gets a unique terminal_seq monotonically increasing
    with launch order (same seq_counter domain).
    """
    rng = random.Random(20260709)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        orch = RunOrchestrator(WorkerPool())
        n_runs = rng.randint(2, 10)
        run_ids = [f"r-{round_idx}-{i}" for i in range(n_runs)]
        _seed_runs(conn, run_ids)

        # Launch all then terminate all in shuffled order.
        for r in run_ids:
            orch.launch(
                conn,
                RunLaunchSpec(run_id=r, session_id="s1", worker_id="w1"),
            )
        shuffled = list(run_ids)
        rng.shuffle(shuffled)

        seqs: dict[str, int] = {}
        for r in shuffled:
            result = orch.terminate(
                conn,
                run_id=r,
                session_id="s1",
                target_status=rng.choice(_TERMINAL_TARGETS),
                cause=rng.choice(_CAUSES),
            )
            assert result.outcome is TerminateOutcome.APPLIED
            seqs[r] = result.terminal_seq

        # All disjoint.
        assert len(set(seqs.values())) == n_runs
        # Every seq is > every launch seq (launch consumed first n allocations).
        assert min(seqs.values()) >= n_runs + 1
        conn.close()


def test_property_reap_orphans_recovers_only_active_runs():
    """Randomly plant M active + K terminal runs; reap must recover exactly M."""
    rng = random.Random(20260710)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        active_count = rng.randint(0, 6)
        terminal_count = rng.randint(0, 6)
        for i in range(active_count):
            conn.execute(
                "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
                "VALUES (?, 's1', 'running', 0, 0)",
                (f"active-{i}",),
            )
        for i in range(terminal_count):
            conn.execute(
                "INSERT INTO runs (run_id, session_id, status, started_at, updated_at) "
                "VALUES (?, 's1', ?, 0, 0)",
                (f"terminal-{i}", rng.choice(("completed", "failed", "cancelled"))),
            )
        conn.commit()

        orch = RunOrchestrator(WorkerPool())
        recovered = orch.reap_orphans(conn)
        assert len(recovered) == active_count, (
            f"round {round_idx}: expected {active_count} recovered, got {len(recovered)}"
        )
        assert orch.pool.size() == active_count
        conn.close()
