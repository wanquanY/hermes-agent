"""Phase E — EventLedger single-appender / cursor / internal-filter (spec §6.4)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.domain.event_ledger import (
    AppendResult,
    EventLedger,
    LedgerEvent,
)


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
    conn.commit()
    return conn


def test_append_allocates_monotonic_seq():
    conn = _make_conn()
    ledger = EventLedger(conn)

    first = ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="message.start",
        payload={"role": "assistant"},
    )
    second = ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="message.complete",
        payload={"status": "completed"},
    )

    assert first.result is AppendResult.APPLIED
    assert first.seq == 1
    assert second.result is AppendResult.APPLIED
    assert second.seq == 2


def test_append_preassigned_seq_is_idempotent_on_duplicate():
    conn = _make_conn()
    ledger = EventLedger(conn)

    first = ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="message.start",
        payload={"role": "assistant"},
        preassigned_seq=7,
    )
    second = ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="message.start",
        payload={"role": "assistant"},
        preassigned_seq=7,
    )

    assert first.result is AppendResult.APPLIED
    assert second.result is AppendResult.IDEMPOTENT_SKIP
    assert second.seq == 7


def test_list_returns_events_in_seq_order():
    conn = _make_conn()
    ledger = EventLedger(conn)
    for i, etype in enumerate(("message.start", "message.delta", "message.complete"), start=1):
        ledger.append(
            session_id="s1",
            run_id="r1",
            event_type=etype,
            payload={"i": i},
        )

    got = ledger.list("s1")
    assert [e.seq for e in got] == [1, 2, 3]
    assert [e.event_type for e in got] == [
        "message.start",
        "message.delta",
        "message.complete",
    ]


def test_list_after_seq_cursor():
    conn = _make_conn()
    ledger = EventLedger(conn)
    for i in range(5):
        ledger.append(
            session_id="s1",
            run_id="r1",
            event_type="message.delta",
            payload={"i": i},
        )

    got = ledger.list("s1", after_seq=2)
    assert [e.seq for e in got] == [3, 4, 5]


def test_list_filters_internal_events_by_default():
    """spec §7.4 — _internal.interaction.* stays out of the default renderer stream."""
    conn = _make_conn()
    ledger = EventLedger(conn)

    ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="message.start",
        payload={},
    )
    ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="_internal.interaction.requested",
        payload={"request_id": "req-1", "kind": "approval"},
    )

    default = ledger.list("s1")
    assert [e.event_type for e in default] == ["message.start"]

    with_internal = ledger.list("s1", include_internal=True)
    assert [e.event_type for e in with_internal] == [
        "message.start",
        "_internal.interaction.requested",
    ]


def test_list_type_filter():
    conn = _make_conn()
    ledger = EventLedger(conn)
    for etype in ("message.start", "tool.start", "tool.complete", "message.complete"):
        ledger.append(session_id="s1", run_id="r1", event_type=etype, payload={})

    got = ledger.list("s1", types={"tool.start", "tool.complete"})
    assert {e.event_type for e in got} == {"tool.start", "tool.complete"}


def test_append_rejects_empty_session_or_type():
    conn = _make_conn()
    ledger = EventLedger(conn)
    with pytest.raises(ValueError):
        ledger.append(session_id="", run_id="r1", event_type="message.start", payload={})
    with pytest.raises(ValueError):
        ledger.append(session_id="s1", run_id="r1", event_type="", payload={})


def test_list_decodes_payload_json():
    conn = _make_conn()
    ledger = EventLedger(conn)
    ledger.append(
        session_id="s1",
        run_id="r1",
        event_type="message.start",
        payload={"role": "assistant", "message_id": "m1"},
    )
    got = ledger.list("s1")
    assert isinstance(got[0], LedgerEvent)
    assert got[0].payload == {"role": "assistant", "message_id": "m1"}
