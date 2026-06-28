from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path: Path) -> SessionDB:
    session_db = SessionDB(tmp_path / "state.db")
    yield session_db
    session_db.close()


def _insert(
    db: SessionDB,
    command_id: str,
    *,
    activity_id: str = "act-1",
    kind: str = "create",
) -> dict:
    return db.insert_activity_command(
        command_id=command_id,
        activity_id=activity_id,
        kind=kind,
        payload={"command": command_id},
        metadata={"test": True},
    )


def _set_intent_at(db: SessionDB, command_id: str, intent_at: float) -> None:
    with db._lock:
        db._conn.execute(
            "UPDATE activity_commands SET intent_at = ? WHERE command_id = ?",
            (intent_at, command_id),
        )


def _run_event_id(db: SessionDB) -> int:
    db.create_session("session-1", "test")
    db.append_run_event("session-1", {"type": "message.start", "payload": {"text": "hi"}})
    with db._lock:
        row = db._conn.execute(
            "SELECT id FROM run_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            ("session-1",),
        ).fetchone()
    assert row is not None
    return int(row["id"])


def test_activity_commands_table_created(db: SessionDB) -> None:
    with db._lock:
        rows = db._conn.execute('PRAGMA table_info("activity_commands")').fetchall()
    column_names = {row["name"] for row in rows}
    assert {
        "command_id",
        "activity_id",
        "kind",
        "payload_json",
        "intent_at",
        "state",
        "state_changed_at",
        "result_event_id",
        "error_reason",
        "metadata_json",
    } <= column_names


def test_activity_commands_indexes_created(db: SessionDB) -> None:
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'index'
               AND name IN (
                    'idx_activity_commands_state',
                    'idx_activity_commands_activity'
               )
            """
        ).fetchall()
    assert {row["name"] for row in rows} == {
        "idx_activity_commands_state",
        "idx_activity_commands_activity",
    }


def test_insert_activity_command_returns_row(db: SessionDB) -> None:
    row = db.insert_activity_command(
        command_id="cmd-1",
        activity_id="act-1",
        kind="create",
        payload={"activity": "act-1"},
        metadata={"source": "pytest"},
    )

    assert row["command_id"] == "cmd-1"
    assert row["activity_id"] == "act-1"
    assert row["kind"] == "create"
    assert row["state"] == "accepted"
    assert row["payload"] == {"activity": "act-1"}
    assert row["metadata"] == {"source": "pytest"}
    assert json.loads(row["payload_json"]) == {"activity": "act-1"}
    assert json.loads(row["metadata_json"]) == {"source": "pytest"}
    assert row["intent_at"] > 0
    assert row["state_changed_at"] >= row["intent_at"]


def test_insert_activity_command_idempotent_on_command_id(db: SessionDB) -> None:
    first = _insert(db, "cmd-1")
    second = db.insert_activity_command(
        command_id="cmd-1",
        activity_id="act-2",
        kind="start",
        payload={"changed": True},
    )

    assert first
    assert second == {}
    assert db.get_activity_command("cmd-1")["activity_id"] == "act-1"


def test_insert_activity_command_rejects_unknown_kind(db: SessionDB) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_activity_command(
            command_id="cmd-1",
            activity_id="act-1",
            kind="pause",
        )


def test_get_activity_command_returns_full_row(db: SessionDB) -> None:
    _insert(db, "cmd-1", kind="start")

    row = db.get_activity_command("cmd-1")

    assert row["command_id"] == "cmd-1"
    assert row["kind"] == "start"
    assert row["payload"] == {"command": "cmd-1"}
    assert row["metadata"] == {"test": True}
    assert row["result_event_id"] is None
    assert row["error_reason"] == ""


def test_get_activity_command_missing_returns_empty(db: SessionDB) -> None:
    assert db.get_activity_command("missing") == {}


def test_list_pending_activity_commands_default_states(db: SessionDB) -> None:
    _insert(db, "accepted")
    _insert(db, "dispatched")
    _insert(db, "satisfied")
    db.update_activity_command_state("dispatched", next_state="dispatched")
    db.update_activity_command_state("satisfied", next_state="satisfied")

    rows = db.list_pending_activity_commands()

    assert [row["command_id"] for row in rows] == ["accepted", "dispatched"]


def test_list_pending_activity_commands_custom_states(db: SessionDB) -> None:
    _insert(db, "failed")
    _insert(db, "satisfied")
    _insert(db, "accepted")
    db.update_activity_command_state("failed", next_state="failed", error_reason="boom")
    db.update_activity_command_state("satisfied", next_state="satisfied")

    rows = db.list_pending_activity_commands(states=("failed", "satisfied"))

    assert [row["command_id"] for row in rows] == ["failed", "satisfied"]


def test_list_pending_activity_commands_orders_by_intent_at(db: SessionDB) -> None:
    _insert(db, "late")
    _insert(db, "early")
    _set_intent_at(db, "late", 20.0)
    _set_intent_at(db, "early", 10.0)

    rows = db.list_pending_activity_commands(states=("accepted",))

    assert [row["command_id"] for row in rows] == ["early", "late"]


def test_update_activity_command_state_legal_transition(db: SessionDB) -> None:
    _insert(db, "cmd-1")

    row = db.update_activity_command_state("cmd-1", next_state="dispatched")

    assert row["command_id"] == "cmd-1"
    assert row["state"] == "dispatched"
    assert db.get_activity_command("cmd-1")["state"] == "dispatched"


def test_update_activity_command_state_rejects_illegal_transition(db: SessionDB) -> None:
    _insert(db, "cmd-1")
    db.update_activity_command_state("cmd-1", next_state="satisfied")

    row = db.update_activity_command_state("cmd-1", next_state="failed")

    assert row == {}
    assert db.get_activity_command("cmd-1")["state"] == "satisfied"


def test_update_activity_command_state_stamps_state_changed_at(db: SessionDB) -> None:
    _insert(db, "cmd-1")
    with db._lock:
        db._conn.execute(
            "UPDATE activity_commands SET state_changed_at = 1 WHERE command_id = ?",
            ("cmd-1",),
        )

    row = db.update_activity_command_state("cmd-1", next_state="dispatched")

    assert row["state_changed_at"] > 1


def test_update_activity_command_state_records_result_event_id(db: SessionDB) -> None:
    _insert(db, "cmd-1")
    event_id = _run_event_id(db)

    row = db.update_activity_command_state(
        "cmd-1",
        next_state="satisfied",
        result_event_id=event_id,
    )

    assert row["result_event_id"] == event_id


def test_list_activity_commands_for_activity_all_states(db: SessionDB) -> None:
    _insert(db, "accepted", activity_id="act-1")
    _insert(db, "dispatched", activity_id="act-1")
    _insert(db, "satisfied", activity_id="act-1")
    _insert(db, "failed", activity_id="act-1")
    _insert(db, "other", activity_id="act-2")
    db.update_activity_command_state("dispatched", next_state="dispatched")
    db.update_activity_command_state("satisfied", next_state="satisfied")
    db.update_activity_command_state("failed", next_state="failed", error_reason="boom")

    rows = db.list_activity_commands_for_activity("act-1")

    assert [row["command_id"] for row in rows] == [
        "accepted",
        "dispatched",
        "satisfied",
        "failed",
    ]
    assert {row["state"] for row in rows} == {
        "accepted",
        "dispatched",
        "satisfied",
        "failed",
    }
