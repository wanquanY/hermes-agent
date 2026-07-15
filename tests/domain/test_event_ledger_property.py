"""Phase K — property-style tests for EventLedger (spec §J3)."""

from __future__ import annotations

import random
import sqlite3
import time

from hermes_agent.domain.event_ledger import AppendResult, EventLedger
from hermes_agent.composition.session_repository_db import (
    ensure_session_repository_schema,
)


PROPERTY_ROUNDS = 24


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
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
    ensure_session_repository_schema(conn)
    conn.commit()
    return conn


def _ensure_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, updated_at) VALUES (?, 'test', 0, 0)",
        (session_id,),
    )


_EVENT_TYPES = (
    "message.start",
    "message.delta",
    "message.complete",
    "tool.start",
    "tool.progress",
    "tool.complete",
    "reasoning.delta",
    "thinking.delta",
    "session.recalled",
)


def test_property_list_returns_seq_ordered_events():
    rng = random.Random(20260711)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        ledger = EventLedger(conn)
        session_id = f"s-{round_idx}"
        _ensure_session(conn, session_id)
        n_events = rng.randint(1, 30)
        for i in range(n_events):
            ledger.append(
                session_id=session_id,
                run_id=f"r-{i % 3}",
                event_type=rng.choice(_EVENT_TYPES),
                payload={"i": i},
                now=time.time() + rng.random(),
            )
        got = ledger.list(session_id, limit=n_events + 10)
        assert [e.seq for e in got] == list(range(1, n_events + 1)), (
            f"round {round_idx}: EventLedger.list must return strict seq order"
        )
        conn.close()


def test_property_preassigned_seq_is_idempotent():
    """Same (session_id, seq) attempted twice → APPLIED once, IDEMPOTENT_SKIP after."""
    rng = random.Random(20260712)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        ledger = EventLedger(conn)
        session_id = f"s-{round_idx}"
        _ensure_session(conn, session_id)
        seq_targets = list(range(1, rng.randint(3, 12) + 1))
        # Ensure seq_counter row exists.
        conn.execute(
            "INSERT INTO seq_counter (session_id, next_seq) VALUES (?, 1)",
            (session_id,),
        )
        first_results = []
        for seq in seq_targets:
            outcome = ledger.append(
                session_id=session_id,
                run_id="r1",
                event_type="message.start",
                payload={},
                preassigned_seq=seq,
            )
            first_results.append(outcome.result)
        for seq in seq_targets:
            outcome = ledger.append(
                session_id=session_id,
                run_id="r1",
                event_type="message.start",
                payload={},
                preassigned_seq=seq,
            )
            assert outcome.result is AppendResult.IDEMPOTENT_SKIP, (
                f"round {round_idx}: seq={seq} must idempotent-skip on repeat"
            )
        assert all(r is AppendResult.APPLIED for r in first_results)


def test_property_internal_events_are_filtered_by_default():
    """No matter how many `_internal.*` events appear, default list omits them."""
    rng = random.Random(20260713)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        ledger = EventLedger(conn)
        session_id = f"s-{round_idx}"
        _ensure_session(conn, session_id)
        internal_count = 0
        visible_count = 0
        for _ in range(rng.randint(5, 25)):
            if rng.random() < 0.4:
                ledger.append(
                    session_id=session_id,
                    run_id="r1",
                    event_type="_internal.interaction.requested",
                    payload={},
                )
                internal_count += 1
            else:
                ledger.append(
                    session_id=session_id,
                    run_id="r1",
                    event_type=rng.choice(_EVENT_TYPES),
                    payload={},
                )
                visible_count += 1
        default = ledger.list(session_id, limit=500)
        assert all(not e.event_type.startswith("_internal.") for e in default), (
            f"round {round_idx}: _internal.* must be filtered from default list"
        )
        assert len(default) == visible_count
        with_internal = ledger.list(session_id, include_internal=True, limit=500)
        assert len(with_internal) == internal_count + visible_count
