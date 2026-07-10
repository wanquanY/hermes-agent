"""ADR-0001 Phase 0 — verify schema migration + backfill + write path.

Covered scenarios:
1. New DB has ``run_events.activity_id`` column (schema migration v37).
2. ``append_run_event`` extracts ``activity_id`` from the frame and writes it.
3. ``record_event`` stamps activity_id from a RunContext onto the frame
   so the column is populated end-to-end.
4. Backfill infers ``activity_id`` for legacy NULL rows from session_id
   structural patterns (team mission node / leader conversation / chat).
5. Backfill is idempotent — second run on a completed DB is cheap.
6. ``list_run_events`` accepts an optional ``activity_id`` filter.
"""

from __future__ import annotations

import json

import pytest

from hermes_agent.domain.run_event_activity_backfill import (
    activity_id_from_event_json,
    activity_id_from_session_id,
)
from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


@pytest.fixture
def db(tmp_path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _ensure_session(db: CliSessionStore, session_id: str) -> None:
    db.sessions.create(session_id, "test")


# ────────────────────────────────────────────────────────────────────────
# 1. Schema migration
# ────────────────────────────────────────────────────────────────────────


def test_run_events_has_activity_id_column(db: CliSessionStore) -> None:
    with db._lock:
        rows = db._conn.execute('PRAGMA table_info("run_events")').fetchall()
    column_names = {row["name"] for row in rows}
    assert "activity_id" in column_names


def test_run_events_activity_id_index_exists(db: CliSessionStore) -> None:
    with db._lock:
        rows = db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_run_events_activity_seq'"
        ).fetchall()
    assert rows, "idx_run_events_activity_seq must be created"


# ────────────────────────────────────────────────────────────────────────
# 2. append_run_event picks activity_id from the frame
# ────────────────────────────────────────────────────────────────────────


def test_append_run_event_writes_activity_id_from_top_level(db: CliSessionStore) -> None:
    _ensure_session(db, "session-A")
    db.runs.append_event(
        "session-A",
        {
            "type": "message.start",
            "activity_id": "mission:abc",
            "payload": {"text": "hi"},
        },
    )
    with db._lock:
        row = db._conn.execute(
            "SELECT activity_id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            ("session-A",),
        ).fetchone()
    assert row is not None
    assert row["activity_id"] == "mission:abc"


def test_append_run_event_writes_activity_id_from_payload(db: CliSessionStore) -> None:
    _ensure_session(db, "session-B")
    db.runs.append_event(
        "session-B",
        {
            "type": "message.start",
            "payload": {"text": "hi", "activity_id": "chat:session-B"},
        },
    )
    with db._lock:
        row = db._conn.execute(
            "SELECT activity_id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            ("session-B",),
        ).fetchone()
    assert row is not None
    assert row["activity_id"] == "chat:session-B"


def test_append_run_event_leaves_activity_id_null_when_unspecified(db: CliSessionStore) -> None:
    _ensure_session(db, "session-C")
    db.runs.append_event(
        "session-C",
        {"type": "message.start", "payload": {"text": "hi"}},
    )
    with db._lock:
        row = db._conn.execute(
            "SELECT activity_id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            ("session-C",),
        ).fetchone()
    assert row is not None
    # No inference at write-time — that's backfill's job. Column stays NULL
    # until either the writer supplies activity_id or the backfill pass runs.
    assert row["activity_id"] is None


# ────────────────────────────────────────────────────────────────────────
# 3. RunContext-driven stamping via record_event
# ────────────────────────────────────────────────────────────────────────


def test_run_context_frame_stamps_activity_id_before_persistence(
    db: CliSessionStore,
) -> None:
    from hermes_team_mission.domain.run_context import RunContext
    from tui_gateway.services import run_control

    _ensure_session(db, "team-session-x")
    ctx = RunContext(
        conversation_session_id="team-session-x",
        participant_id="member:alice",
        activity_id="mission:abc",
        activity_kind="mission",
        execution_scope_key="member-chat:conv:alice",
        control_home="/tmp/control",
        execution_home="/tmp/exec",
    )
    frame = run_control._apply_run_context_to_frame(
        {
            "type": "message.start",
            "conversation_session_id": "team-session-x",
            "payload": {"text": "hi"},
        },
        ctx,
    )
    db.runs.append_event("team-session-x", frame)
    with db._lock:
        row = db._conn.execute(
            "SELECT activity_id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            ("team-session-x",),
        ).fetchone()
    assert row is not None
    assert row["activity_id"] == "mission:abc"


# ────────────────────────────────────────────────────────────────────────
# 4. Backfill inference logic
# ────────────────────────────────────────────────────────────────────────


def test_inference_node_session_returns_mission_id() -> None:
    assert (
        activity_id_from_session_id("team:mission-abc123:node:foo")
        == "mission:mission-abc123"
    )


def test_inference_leader_session_returns_team_conversation_id() -> None:
    sid = "team-session-team-conversation-abc-123"
    assert activity_id_from_session_id(sid) == "team-conversation:abc-123"


def test_inference_chat_session_uses_chat_prefix() -> None:
    assert activity_id_from_session_id("20260101_session_xyz") == "chat:20260101_session_xyz"


def test_inference_blank_session_returns_empty() -> None:
    assert activity_id_from_session_id("") == ""


def test_inference_from_event_json_top_level() -> None:
    raw = json.dumps({"type": "message.start", "activity_id": "mission:zzz"})
    assert activity_id_from_event_json(raw) == "mission:zzz"


def test_inference_from_event_json_payload() -> None:
    raw = json.dumps({"type": "tool.complete", "payload": {"activity_id": "chat:s1"}})
    assert activity_id_from_event_json(raw) == "chat:s1"


def test_inference_from_event_json_missing_returns_empty() -> None:
    raw = json.dumps({"type": "message.start", "payload": {"text": "hi"}})
    assert activity_id_from_event_json(raw) == ""


# ────────────────────────────────────────────────────────────────────────
# 5. End-to-end backfill on a populated DB
# ────────────────────────────────────────────────────────────────────────


def test_backfill_populates_legacy_rows(db: CliSessionStore) -> None:
    _ensure_session(db, "team:mission-xyz:node:a")
    _ensure_session(db, "team-session-team-conversation-conv-9")
    _ensure_session(db, "20260201_user_chat")
    db.runs.append_event(
        "team:mission-xyz:node:a",
        {"type": "message.start", "payload": {"text": "hi"}},
    )
    db.runs.append_event(
        "team-session-team-conversation-conv-9",
        {"type": "message.start", "payload": {"text": "hi"}},
    )
    db.runs.append_event(
        "20260201_user_chat",
        {"type": "message.start", "payload": {"text": "hi"}},
    )

    result = db.run_event_maintenance.backfill_activity_ids(batch_size=100)
    assert result["scanned"] == 3
    assert result["updated"] == 3
    assert result["done"] is True

    with db._lock:
        rows = db._conn.execute(
            "SELECT session_id, activity_id FROM run_events ORDER BY id"
        ).fetchall()
    inferred = {row["session_id"]: row["activity_id"] for row in rows}
    assert inferred["team:mission-xyz:node:a"] == "mission:mission-xyz"
    assert inferred["team-session-team-conversation-conv-9"] == "team-conversation:conv-9"
    assert inferred["20260201_user_chat"] == "chat:20260201_user_chat"


def test_backfill_is_idempotent(db: CliSessionStore) -> None:
    _ensure_session(db, "20260201_x")
    db.runs.append_event("20260201_x", {"type": "message.start"})
    first = db.run_event_maintenance.backfill_activity_ids()
    assert first["done"] is True

    # Second call should be marked as skipped (done-marker is set).
    second = db.run_event_maintenance.backfill_activity_ids()
    assert second["skipped"] is True
    assert second["reason"] == "done"


# ────────────────────────────────────────────────────────────────────────
# 6. list_run_events optional activity_id filter
# ────────────────────────────────────────────────────────────────────────


def test_list_run_events_filters_by_activity_id(db: CliSessionStore) -> None:
    _ensure_session(db, "s1")
    db.runs.append_event(
        "s1",
        {"type": "message.start", "activity_id": "mission:m1", "payload": {"text": "a"}},
    )
    db.runs.append_event(
        "s1",
        {"type": "message.complete", "activity_id": "mission:m2", "payload": {"text": "b"}},
    )
    db.runs.append_event(
        "s1",
        {"type": "message.start", "activity_id": "mission:m1", "payload": {"text": "c"}},
    )

    only_m1 = db.runs.list_events("s1", activity_id="mission:m1")
    assert len(only_m1) == 2
    assert all(
        (e.get("activity_id") or e.get("activityId") or e.get("payload", {}).get("activity_id"))
        == "mission:m1"
        for e in only_m1
    )

    only_m2 = db.runs.list_events("s1", activity_id="mission:m2")
    assert len(only_m2) == 1


def test_list_run_events_without_activity_filter_returns_all(db: CliSessionStore) -> None:
    _ensure_session(db, "s2")
    db.runs.append_event("s2", {"type": "message.start", "activity_id": "mission:m1"})
    db.runs.append_event("s2", {"type": "message.complete", "activity_id": "mission:m2"})
    all_events = db.runs.list_events("s2")
    assert len(all_events) == 2
