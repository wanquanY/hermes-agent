from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def test_create_activity_inserts_pending_row(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.activities.create(
        activity_id="act-1",
        conversation_id="conv-1",
        kind="agent_dispatch",
        parent_activity_id="parent-1",
        target_profile_id="profile-1",
        target_mission_id="mission-1",
        prompt_summary="Investigate the failure",
        notify_parent=False,
    )

    assert row["activity_id"] == "act-1"
    assert row["conversation_id"] == "conv-1"
    assert row["kind"] == "agent_dispatch"
    assert row["status"] == "pending"
    assert row["parent_activity_id"] == "parent-1"
    assert row["target_profile_id"] == "profile-1"
    assert row["target_mission_id"] == "mission-1"
    assert row["prompt_summary"] == "Investigate the failure"
    assert row["notify_parent"] is False
    assert row["read_at"] is None
    assert row["created_at"] > 0
    assert row["updated_at"] >= row["created_at"]


def test_update_activity_status_transitions_running(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="chat")

    assert db.activities.update_status("act-1", "running", started_at=123.0)

    row = db.activities.get("act-1")
    assert row is not None
    assert row["status"] == "running"
    assert row["started_at"] == 123.0
    assert row["completed_at"] is None
    assert row["updated_at"] >= row["created_at"]


def test_mark_activity_completed_sets_result_fields(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="agent_dispatch")

    assert db.activities.mark_completed(
        "act-1",
        result_summary="Done",
        result_json={"ok": True, "items": [1, 2]},
    )

    row = db.activities.get("act-1")
    assert row is not None
    assert row["status"] == "completed"
    assert row["result_summary"] == "Done"
    assert json.loads(row["result_json"]) == {"ok": True, "items": [1, 2]}
    assert row["completed_at"] is not None


def test_mark_activity_failed_sets_error_summary(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="team_dispatch")

    assert db.activities.mark_failed("act-1", error_message="worker crashed")

    row = db.activities.get("act-1")
    assert row is not None
    assert row["status"] == "failed"
    assert row["result_summary"] == "worker crashed"
    assert row["completed_at"] is not None


def test_mark_activity_cancelled_clears_inflight(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="member_chat")
    db.activities.update_status("act-1", "running", started_at=10.0)

    assert db.activities.mark_cancelled("act-1")

    row = db.activities.get("act-1")
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["completed_at"] is not None
    assert db.activities.list("conv-1", status="running") == []


def test_mark_activity_read_sets_read_at(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.mark_completed("act-1", result_summary="Done", result_json="{}")

    assert db.activities.mark_read("act-1")

    row = db.activities.get("act-1")
    assert row is not None
    assert row["read_at"] is not None
    assert row["updated_at"] >= row["read_at"]


def test_list_activities_filters_by_conversation_and_status(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="conv1-pending", conversation_id="conv-1", kind="chat")
    db.activities.create(activity_id="conv1-running", conversation_id="conv-1", kind="chat")
    db.activities.create(activity_id="conv2-running", conversation_id="conv-2", kind="chat")
    db.activities.update_status("conv1-running", "running")
    db.activities.update_status("conv2-running", "running")

    assert [row["activity_id"] for row in db.activities.list("conv-1")] == [
        "conv1-pending",
        "conv1-running",
    ]
    assert [row["activity_id"] for row in db.activities.list("conv-1", status="running")] == [
        "conv1-running",
    ]


def test_list_unread_completions_excludes_read(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="unread", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.create(activity_id="read", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.create(activity_id="pending", conversation_id="conv-1", kind="agent_dispatch")
    db.activities.mark_completed("unread", result_summary="Done", result_json="{}")
    db.activities.mark_failed("read", error_message="failed")
    db.activities.mark_read("read")

    assert [row["activity_id"] for row in db.activities.list_unread(conversation_id="conv-1")] == [
        "unread",
    ]


def test_get_unread_completion_count_by_parent_and_by_conv(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="parent", conversation_id="conv-1", kind="chat")
    db.activities.create(
        activity_id="child-unread",
        conversation_id="conv-1",
        parent_activity_id="parent",
        kind="agent_dispatch",
    )
    db.activities.create(
        activity_id="child-read",
        conversation_id="conv-1",
        parent_activity_id="parent",
        kind="team_dispatch",
    )
    db.activities.create(activity_id="other", conversation_id="conv-1", kind="member_chat")
    db.activities.create(activity_id="other-conv", conversation_id="conv-2", kind="agent_dispatch")
    for activity_id in ("child-unread", "child-read", "other", "other-conv"):
        db.activities.mark_completed(activity_id, result_summary="Done", result_json="{}")
    db.activities.mark_read("child-read")

    assert db.activities.unread_count(parent_activity_id="parent") == 1
    assert db.activities.unread_count(conversation_id="conv-1") == 2


def test_invalid_kind_raises_check_constraint(tmp_path: Path) -> None:
    db = _db(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="not_a_kind")


def test_invalid_status_raises_check_constraint(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.create(activity_id="act-1", conversation_id="conv-1", kind="chat")

    with pytest.raises(sqlite3.IntegrityError):
        db.activities.update_status("act-1", "interrupted")
