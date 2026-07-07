"""Phase K — property-style tests for SeqAllocator (spec §J2).

Uses stdlib ``random`` in place of Hypothesis since the venv does not carry
``hypothesis``. Each test runs N randomized rounds and asserts the invariant
for every round.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_agent.domain.exceptions import SeqAllocatorBusy
from hermes_agent.domain.seq_allocator import (
    allocate_only,
    backfill_seq_counter,
    ensure_seq_counter_table,
)


PROPERTY_ROUNDS = 32


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            UNIQUE(session_id, seq)
        );
        """
    )
    ensure_seq_counter_table(conn)
    return conn


def test_property_allocate_only_yields_strictly_monotonic_sequence():
    """Repeated allocations always produce seq_{n+1} = seq_n + 1."""
    rng = random.Random(20260707)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        session_id = f"s-{round_idx}"
        conn.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
        conn.commit()

        n_allocs = rng.randint(1, 40)
        allocated: list[int] = []
        for _ in range(n_allocs):
            seq = allocate_only(conn, session_id=session_id, updated_at=time.time())
            allocated.append(seq)

        assert allocated == list(range(1, n_allocs + 1)), (
            f"round {round_idx}: expected strict 1..N monotonicity, got {allocated}"
        )
        conn.close()


def test_property_allocate_only_no_collision_across_sessions():
    """Independent sessions each get their own 1..N monotonic sequence."""
    rng = random.Random(20260708)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        session_count = rng.randint(2, 6)
        sessions = [f"s-{round_idx}-{k}" for k in range(session_count)]
        for sid in sessions:
            conn.execute("INSERT INTO sessions (id) VALUES (?)", (sid,))
        conn.commit()

        alloc_by_session: dict[str, list[int]] = {sid: [] for sid in sessions}
        for _ in range(rng.randint(10, 60)):
            sid = rng.choice(sessions)
            seq = allocate_only(conn, session_id=sid, updated_at=time.time())
            alloc_by_session[sid].append(seq)

        for sid, seqs in alloc_by_session.items():
            assert seqs == list(range(1, len(seqs) + 1)), (
                f"round {round_idx}: session {sid} lost monotonicity"
            )
        conn.close()


def test_property_backfill_then_allocate_continues_past_max_seq():
    """After backfill from existing run_events, next allocation continues from MAX(seq)+1."""
    rng = random.Random(20260709)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        session_id = f"s-{round_idx}"
        conn.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
        max_existing = rng.randint(1, 20)
        for seq in range(1, max_existing + 1):
            conn.execute(
                "INSERT INTO run_events (session_id, seq) VALUES (?, ?)",
                (session_id, seq),
            )
        conn.commit()

        backfill_seq_counter(conn, updated_at=time.time())
        next_seq = allocate_only(conn, session_id=session_id, updated_at=time.time())

        assert next_seq == max_existing + 1, (
            f"round {round_idx}: expected next_seq={max_existing + 1}, got {next_seq}"
        )
        conn.close()


def test_property_concurrent_allocation_produces_no_gap_or_collision():
    """Multiple threads allocating on the same session get a disjoint 1..N cover.

    We serialize DB access via a per-thread connection (SQLite can't share a
    connection across threads without check_same_thread=False) and use a
    file-backed DB so all workers see the same seq_counter row.
    """
    import os
    import tempfile

    rng = random.Random(20260710)
    for round_idx in range(4):  # threaded runs are more expensive
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "state.db")
            with sqlite3.connect(db_path) as bootstrap:
                bootstrap.execute("PRAGMA journal_mode=WAL")
                bootstrap.execute("PRAGMA busy_timeout=5000")
                bootstrap.executescript(
                    """
                    CREATE TABLE sessions (id TEXT PRIMARY KEY);
                    CREATE TABLE run_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        UNIQUE(session_id, seq)
                    );
                    """
                )
                ensure_seq_counter_table(bootstrap)
                bootstrap.execute("INSERT INTO sessions (id) VALUES ('sX')")
                bootstrap.commit()

            worker_count = rng.randint(4, 8)
            allocs_per_worker = rng.randint(5, 15)
            results: list[int] = []
            lock = threading.Lock()

            def _worker():
                conn = sqlite3.connect(db_path, timeout=5.0)
                conn.execute("PRAGMA busy_timeout=5000")
                local_allocated: list[int] = []
                try:
                    for _ in range(allocs_per_worker):
                        for attempt in range(5):
                            try:
                                seq = allocate_only(
                                    conn,
                                    session_id="sX",
                                    updated_at=time.time(),
                                )
                                local_allocated.append(seq)
                                conn.commit()
                                break
                            except SeqAllocatorBusy:
                                time.sleep(0.01)
                        else:
                            raise AssertionError("SeqAllocator retries exhausted")
                finally:
                    conn.close()
                with lock:
                    results.extend(local_allocated)

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [pool.submit(_worker) for _ in range(worker_count)]
                for f in futures:
                    f.result()

            expected_total = worker_count * allocs_per_worker
            assert sorted(results) == list(range(1, expected_total + 1)), (
                f"round {round_idx}: gap or collision — expected 1..{expected_total}, "
                f"got {sorted(results)}"
            )
