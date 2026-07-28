"""Phase K — property-style tests for RunRepoImpl (spec §4.2)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.domain.run_terminator import TerminateCause, TerminateOutcome
from hermes_agent.repositories import (
    CanonicalEventSpec,
    RunRepoImpl,
    RunSpec,
)


PROPERTY_ROUNDS = 24


_EVENT_TYPES = (
    "message.start",
    "message.delta",
    "message.complete",
    "tool.start",
    "tool.progress",
    "tool.complete",
    "reasoning.delta",
    "thinking.delta",
)


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
            timestamp REAL NOT NULL,
            payload_json TEXT,
            event_json TEXT NOT NULL,
            activity_id TEXT,
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


def test_property_append_event_returns_strict_monotonic_seq():
    rng = random.Random(20260721)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = RunRepoImpl(conn)
        repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
        n = rng.randint(1, 30)
        seqs = []
        for _ in range(n):
            seq = repo.append_event(
                "s1",
                CanonicalEventSpec(
                    event_type=rng.choice(_EVENT_TYPES),
                    payload={"noise": rng.random()},
                    run_id="r1",
                ),
            )
            seqs.append(seq)
        assert seqs == list(range(1, n + 1))
        conn.close()


def test_property_list_events_returns_in_seq_order_regardless_of_type_mix():
    rng = random.Random(20260722)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = RunRepoImpl(conn)
        repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
        n = rng.randint(1, 25)
        for _ in range(n):
            repo.append_event(
                "s1",
                CanonicalEventSpec(
                    event_type=rng.choice(_EVENT_TYPES),
                    payload={},
                    run_id="r1",
                ),
            )
        events = repo.list_events("s1", limit=n + 5)
        assert [e.seq for e in events] == list(range(1, n + 1))
        conn.close()


def test_property_list_tool_events_returns_only_tool_prefix():
    rng = random.Random(20260723)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = RunRepoImpl(conn)
        repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
        tool_count = 0
        for _ in range(rng.randint(5, 25)):
            etype = rng.choice(_EVENT_TYPES)
            repo.append_event(
                "s1",
                CanonicalEventSpec(event_type=etype, payload={}, run_id="r1"),
            )
            if etype.startswith("tool."):
                tool_count += 1
        tool_events = repo.list_tool_events("s1", limit=500)
        assert len(tool_events) == tool_count
        for e in tool_events:
            assert e.event_type.startswith("tool.")
        conn.close()


def test_property_set_terminal_random_re_calls_stay_idempotent():
    rng = random.Random(20260724)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = RunRepoImpl(conn)
        repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
        # Append some pre-terminal events.
        for _ in range(rng.randint(0, 5)):
            repo.append_event(
                "s1",
                CanonicalEventSpec(
                    event_type=rng.choice(_EVENT_TYPES),
                    payload={},
                    run_id="r1",
                ),
            )
        first_target = rng.choice(("completed", "failed", "interrupted", "cancelled"))
        first = repo.set_terminal(
            run_id="r1",
            session_id="s1",
            target_status=first_target,
            cause=rng.choice(list(TerminateCause)),
        )
        assert first.outcome is TerminateOutcome.APPLIED

        for _ in range(rng.randint(2, 12)):
            follow = repo.set_terminal(
                run_id="r1",
                session_id="s1",
                target_status=rng.choice(
                    ("completed", "failed", "interrupted", "cancelled")
                ),
                cause=rng.choice(list(TerminateCause)),
            )
            assert follow.outcome is TerminateOutcome.IDEMPOTENT_SKIP
            assert follow.terminal_status == first_target
        conn.close()


def test_property_multi_session_seq_domains_are_independent():
    """Two sessions' seqs should each start at 1 and be strictly monotonic
    within their own domain, no cross-contamination.
    """
    rng = random.Random(20260725)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        conn.execute("INSERT INTO sessions (id) VALUES ('s2')")
        repo = RunRepoImpl(conn)
        repo.create_run("s1", RunSpec(run_id="r1", session_id="s1"))
        repo.create_run("s2", RunSpec(run_id="r2", session_id="s2"))
        s1_seqs = []
        s2_seqs = []
        # Random interleaving.
        for _ in range(rng.randint(4, 20)):
            if rng.random() < 0.5:
                s = repo.append_event(
                    "s1",
                    CanonicalEventSpec(
                        event_type="message.delta", payload={}, run_id="r1"
                    ),
                )
                s1_seqs.append(s)
            else:
                s = repo.append_event(
                    "s2",
                    CanonicalEventSpec(
                        event_type="message.delta", payload={}, run_id="r2"
                    ),
                )
                s2_seqs.append(s)
        assert s1_seqs == list(range(1, len(s1_seqs) + 1))
        assert s2_seqs == list(range(1, len(s2_seqs) + 1))
        conn.close()
