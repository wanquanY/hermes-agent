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
            turn_id TEXT,
            runtime_session_id TEXT,
            runtime_scope_key TEXT,
            participant_id TEXT,
            activity_id TEXT,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            timestamp REAL NOT NULL,
            payload_json TEXT,
            event_json TEXT NOT NULL,
            status TEXT,
            frame_blob BLOB,
            frame_format TEXT,
            retention_class TEXT,
            interaction_request_id TEXT,
            interaction_kind TEXT,
            interaction_status TEXT,
            anchor_seq INTEGER NOT NULL DEFAULT 0,
            projected_message_id TEXT,
            projected_tool_event_id TEXT,
            projection_state TEXT,
            runtime_source_seq INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session_id, seq)
        );
        CREATE TABLE seq_counter (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE tool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            tool_call_id TEXT,
            tool_name TEXT,
            status TEXT,
            seq_start INTEGER,
            seq_last INTEGER,
            payload_json TEXT
        );
        CREATE TABLE run_event_search_index (
            run_event_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            runtime_scope_key TEXT,
            runtime_source_seq INTEGER,
            search_text TEXT,
            updated_at REAL
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


def test_append_runtime_frame_persists_full_gateway_columns():
    conn = _make_conn()
    ledger = EventLedger(conn)

    ledger.append_runtime_frame(
        session_id="s1",
        run_id="r1",
        turn_id="t1",
        runtime_session_id="runtime-s1",
        runtime_scope_key="scope:s1",
        participant_id="participant:leader",
        activity_id="activity-1",
        event_type="_internal.interaction.requested",
        seq=3,
        timestamp=123.5,
        payload_json='{"request_id":"req-1"}',
        event_json='{"type":"_internal.interaction.requested","seq":3}',
        status="pending",
        frame_blob=b"frame",
        frame_format="json",
        retention_class="control",
        interaction_request_id="req-1",
        interaction_kind="approval",
        interaction_status="pending",
        anchor_seq=2,
        projection_state="raw",
        runtime_source_seq=99,
    )

    row = conn.execute("SELECT * FROM run_events WHERE session_id = 's1'").fetchone()
    assert row["run_id"] == "r1"
    assert row["turn_id"] == "t1"
    assert row["runtime_session_id"] == "runtime-s1"
    assert row["runtime_scope_key"] == "scope:s1"
    assert row["participant_id"] == "participant:leader"
    assert row["activity_id"] == "activity-1"
    assert row["event_type"] == "_internal.interaction.requested"
    assert row["seq"] == 3
    assert row["status"] == "pending"
    assert row["frame_blob"] == b"frame"
    assert row["frame_format"] == "json"
    assert row["retention_class"] == "control"
    assert row["interaction_request_id"] == "req-1"
    assert row["interaction_kind"] == "approval"
    assert row["interaction_status"] == "pending"
    assert row["anchor_seq"] == 2
    assert row["projection_state"] == "raw"
    assert row["runtime_source_seq"] == 99


def test_projection_markers_update_runtime_frame_row():
    conn = _make_conn()
    ledger = EventLedger(conn)
    ledger.append_runtime_frame(
        session_id="s1",
        run_id="r1",
        turn_id="t1",
        runtime_session_id="runtime-s1",
        runtime_scope_key="scope:s1",
        participant_id="",
        activity_id=None,
        event_type="message.complete",
        seq=1,
        timestamp=1,
        payload_json="{}",
        event_json='{"type":"message.complete","seq":1}',
        status="completed",
        frame_blob=None,
        frame_format="",
        retention_class="",
    )
    row_id = conn.execute("SELECT id FROM run_events WHERE session_id = 's1'").fetchone()["id"]

    ledger.mark_projected_tool_event(row_id=row_id, tool_event_id="tool-event-1")
    ledger.mark_projected_message(row_id=row_id, conversation_message_id="message-1")

    row = conn.execute("SELECT * FROM run_events WHERE id = ?", (row_id,)).fetchone()
    assert row["projected_tool_event_id"] == "tool-event-1"
    assert row["projected_message_id"] == "message-1"
    assert row["projection_state"] == "projected"


def test_maintenance_updates_frame_activity_runtime_source_and_referenced_projection():
    conn = _make_conn()
    ledger = EventLedger(conn)
    ledger.append_runtime_frame(
        session_id="s1",
        run_id="r1",
        turn_id="t1",
        runtime_session_id="runtime-s1",
        runtime_scope_key="scope:s1",
        participant_id="",
        activity_id=None,
        event_type="message.complete",
        seq=1,
        timestamp=1,
        payload_json="{}",
        event_json='{"type":"message.complete","seq":1}',
        status="completed",
        frame_blob=None,
        frame_format="",
        retention_class="",
    )
    row_id = conn.execute("SELECT id FROM run_events WHERE session_id = 's1'").fetchone()["id"]

    ledger.update_frame_columns(
        row_id=row_id,
        frame_blob=b"frame-v2",
        frame_format="zlib+json:v1",
        retention_class="full",
        projection_state="raw",
    )
    ledger.mark_activity_id(row_id=row_id, activity_id="activity-1")
    ledger.update_runtime_source_seq(row_id=row_id, runtime_source_seq=42)
    ledger.rewrite_referenced_frame(
        row_id=row_id,
        payload_json='{"content":"hello"}',
        event_json='{"type":"message.complete","seq":1,"payload":{"content":"hello"}}',
        frame_blob=b"frame-v3",
        frame_format="zlib+json:v1",
        projected_message_id="message-1",
        projected_tool_event_id="tool-1",
        runtime_source_seq=43,
    )

    row = conn.execute("SELECT * FROM run_events WHERE id = ?", (row_id,)).fetchone()
    assert row["frame_blob"] == b"frame-v3"
    assert row["frame_format"] == "zlib+json:v1"
    assert row["retention_class"] == "full"
    assert row["activity_id"] == "activity-1"
    assert row["runtime_source_seq"] == 43
    assert row["payload_json"] == '{"content":"hello"}'
    assert row["projected_message_id"] == "message-1"
    assert row["projected_tool_event_id"] == "tool-1"
    assert row["projection_state"] == "referenced"


def test_compaction_rewrite_preserves_existing_retention_and_deletes_rows():
    conn = _make_conn()
    ledger = EventLedger(conn)
    for seq in (1, 2, 3):
        ledger.append_runtime_frame(
            session_id="s1",
            run_id="r1",
            turn_id="t1",
            runtime_session_id="runtime-s1",
            runtime_scope_key="scope:s1",
            participant_id="old",
            activity_id=None,
            event_type="message.delta",
            seq=seq,
            timestamp=float(seq),
            payload_json="{}",
            event_json=f'{{"type":"message.delta","seq":{seq}}}',
            status="",
            frame_blob=None,
            frame_format="",
            retention_class="existing",
        )
    rows = conn.execute("SELECT id FROM run_events ORDER BY id").fetchall()
    keep_id = int(rows[0]["id"])
    delete_ids = [int(row["id"]) for row in rows[1:]]

    ledger.rewrite_compacted_frame_row(
        row_id=keep_id,
        run_id="r2",
        turn_id="t2",
        runtime_session_id="runtime-s2",
        runtime_scope_key="scope:s2",
        activity_id="activity-2",
        seq=10,
        timestamp=10,
        participant_id="participant-2",
        payload_json='{"merged":true}',
        event_json='{"type":"message.delta","seq":10}',
        frame_blob=b"merged",
        frame_format="zlib+json:v1",
        retention_class="new",
        runtime_source_seq=9,
    )
    deleted = ledger.delete_rows_by_id(delete_ids)

    assert deleted == 2
    remaining = conn.execute("SELECT * FROM run_events").fetchall()
    assert len(remaining) == 1
    row = remaining[0]
    assert row["id"] == keep_id
    assert row["seq"] == 10
    assert row["run_id"] == "r2"
    assert row["turn_id"] == "t2"
    assert row["runtime_session_id"] == "runtime-s2"
    assert row["runtime_scope_key"] == "scope:s2"
    assert row["activity_id"] == "activity-2"
    assert row["participant_id"] == "participant-2"
    assert row["retention_class"] == "existing"
    assert row["runtime_source_seq"] == 9


def test_delete_sessions_matches_visible_and_runtime_session_ids():
    conn = _make_conn()
    ledger = EventLedger(conn)
    ledger.append_runtime_frame(
        session_id="visible-s1",
        run_id="r1",
        turn_id="t1",
        runtime_session_id="runtime-s1",
        runtime_scope_key="scope:s1",
        participant_id="",
        activity_id=None,
        event_type="message.complete",
        seq=1,
        timestamp=1,
        payload_json="{}",
        event_json='{"type":"message.complete","seq":1}',
        status="completed",
        frame_blob=None,
        frame_format="",
        retention_class="",
    )
    ledger.append_runtime_frame(
        session_id="visible-s2",
        run_id="r2",
        turn_id="t2",
        runtime_session_id="runtime-s2",
        runtime_scope_key="scope:s2",
        participant_id="",
        activity_id=None,
        event_type="message.complete",
        seq=1,
        timestamp=1,
        payload_json="{}",
        event_json='{"type":"message.complete","seq":1}',
        status="completed",
        frame_blob=None,
        frame_format="",
        retention_class="",
    )

    assert ledger.delete_sessions(["runtime-s1"]) == 1

    rows = conn.execute("SELECT session_id FROM run_events ORDER BY session_id").fetchall()
    assert [row["session_id"] for row in rows] == ["visible-s2"]


def test_runtime_row_queries_apply_scope_run_activity_and_internal_filters():
    conn = _make_conn()
    ledger = EventLedger(conn)
    ledger.append_runtime_frame(
        session_id="s1",
        run_id="run-1",
        turn_id="t1",
        runtime_session_id="runtime-s1",
        runtime_scope_key="scope-a",
        participant_id="p1",
        activity_id="activity-a",
        event_type="message.start",
        seq=1,
        timestamp=1,
        payload_json="{}",
        event_json='{"type":"message.start","seq":1}',
        status="running",
        frame_blob=None,
        frame_format="",
        retention_class="",
    )
    ledger.append_runtime_frame(
        session_id="s1",
        run_id="run-2",
        turn_id="t2",
        runtime_session_id="runtime-s1",
        runtime_scope_key="scope-b",
        participant_id="p1",
        activity_id="activity-b",
        event_type="_internal.interaction.requested",
        seq=2,
        timestamp=2,
        payload_json="{}",
        event_json='{"type":"_internal.interaction.requested","seq":2}',
        status="pending",
        frame_blob=None,
        frame_format="",
        retention_class="control",
    )

    rows = ledger.list_runtime_rows(
        "s1",
        runtime_scope_key="scope-a",
        run_id="run-1",
        activity_id="activity-a",
    )
    assert [row["seq"] for row in rows] == [1]
    assert [row["seq"] for row in ledger.list_runtime_rows("s1")] == [1]
    assert [row["seq"] for row in ledger.list_runtime_rows("s1", include_internal=True)] == [1, 2]
    assert [row["seq"] for row in ledger.list_activity_rows("activity-a")] == [1]


def test_tool_event_projection_rows_are_ordered_and_tail_bounded():
    conn = _make_conn()
    ledger = EventLedger(conn)
    conn.executemany(
        """
        INSERT INTO tool_events (
            session_id, run_id, tool_call_id, tool_name, status, seq_start, seq_last, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("s1", "run-1", "tool-1", "search", "completed", 1, 3, "{}"),
            ("s1", "run-1", "tool-2", "write", "completed", 4, 6, "{}"),
            ("s1", "run-2", "tool-3", "other", "completed", 7, 8, "{}"),
        ],
    )

    rows = ledger.list_tool_event_projection_rows("s1", run_id="run-1")
    assert [row["tool_call_id"] for row in rows] == ["tool-1", "tool-2"]
    tail = ledger.list_tool_event_projection_rows("s1", direction="tail", limit=2)
    assert [row["tool_call_id"] for row in tail] == ["tool-2", "tool-3"]


def test_filtered_rows_support_type_prefix_explicit_types_scope_and_search_index():
    conn = _make_conn()
    ledger = EventLedger(conn)
    for seq, event_type, scope, payload in (
        (1, "subagent.start", "scope-a", '{"text":"alpha"}'),
        (2, "subagent.reasoning_delta", "scope-a", '{"text":"needle"}'),
        (3, "message.delta", "scope-b", '{"text":"needle"}'),
    ):
        ledger.append_runtime_frame(
            session_id="s1",
            run_id="run-1",
            turn_id="t1",
            runtime_session_id="runtime-s1",
            runtime_scope_key=scope,
            participant_id="",
            activity_id=None,
            event_type=event_type,
            seq=seq,
            timestamp=seq,
            payload_json=payload,
            event_json=f'{{"type":"{event_type}","seq":{seq},"payload":{payload}}}',
            status="",
            frame_blob=None,
            frame_format="",
            retention_class="",
        )
        row_id = conn.execute(
            "SELECT id FROM run_events WHERE session_id = ? AND seq = ?",
            ("s1", seq),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO run_event_search_index (
                run_event_id, session_id, seq, event_type, runtime_scope_key,
                runtime_source_seq, search_text, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row_id, "s1", seq, event_type, scope, 0, payload, float(seq)),
        )

    assert [row["seq"] for row in ledger.list_filtered_rows("s1", event_type_prefix="subagent.")] == [1, 2]
    assert [row["seq"] for row in ledger.list_filtered_rows("s1", event_types=["message.delta"])] == [3]
    assert [row["seq"] for row in ledger.list_filtered_rows("s1", runtime_scope_key="scope-a")] == [1, 2]
    assert [row["seq"] for row in ledger.list_filtered_rows("s1", payload_contains="needle")] == [2, 3]


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
