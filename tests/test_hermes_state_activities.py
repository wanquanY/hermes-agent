from __future__ import annotations

from pathlib import Path

from hermes_state import SessionDB


def _db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def test_activity_status_completed_to_cancelled_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.create_activity(activity_id="act-1", conversation_id="conv-1", kind="chat")
    assert db.mark_activity_completed("act-1", result_summary="Done", result_json={})

    assert db.mark_activity_cancelled("act-1") is False

    row = db.get_activity("act-1")
    assert row is not None
    assert row["status"] == "completed"


def test_activity_status_cancelled_to_completed_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.create_activity(activity_id="act-1", conversation_id="conv-1", kind="chat")
    assert db.mark_activity_cancelled("act-1")

    assert db.mark_activity_completed("act-1", result_summary="Late", result_json={}) is False

    row = db.get_activity("act-1")
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["result_summary"] is None


def test_activity_status_pending_to_cancelled_ok(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.create_activity(activity_id="act-1", conversation_id="conv-1", kind="chat")

    assert db.mark_activity_cancelled("act-1")

    row = db.get_activity("act-1")
    assert row is not None
    assert row["status"] == "cancelled"


def test_activity_status_running_to_cancelled_ok(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.create_activity(activity_id="act-1", conversation_id="conv-1", kind="chat")
    assert db.update_activity_status("act-1", "running", started_at=10.0)

    assert db.mark_activity_cancelled("act-1")

    row = db.get_activity("act-1")
    assert row is not None
    assert row["status"] == "cancelled"


def test_activity_cancel_blocks_late_completed_race(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.create_activity(activity_id="act-1", conversation_id="conv-1", kind="chat")
    assert db.update_activity_status("act-1", "running", started_at=10.0)
    assert db.mark_activity_cancelled("act-1")

    assert db.mark_activity_completed("act-1", result_summary="Late completion", result_json={}) is False

    row = db.get_activity("act-1")
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["result_summary"] is None
