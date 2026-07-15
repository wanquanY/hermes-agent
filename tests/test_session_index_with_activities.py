from __future__ import annotations

import importlib
from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway import server


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _upsert_index_row(
    db: CliSessionStore,
    *,
    session_id: str = "stored-session-1",
    conversation_id: str = "conversation-1",
) -> None:
    db.session_index.upsert(
        session_id=session_id,
        title="Conversation",
        preview="preview",
        source="tui",
        conversation_id=conversation_id,
        started_at=1.0,
        updated_at=1.0,
    )


def _session_item(db: CliSessionStore, session_id: str = "stored-session-1") -> dict:
    rows = {
        item["session_id"]: item
        for item in db.session_index.list()["sessions"]
    }
    return rows[session_id]


def _create_activity(
    db: CliSessionStore,
    activity_id: str,
    *,
    conversation_id: str = "conversation-1",
    kind: str = "agent_dispatch",
) -> None:
    db.activities.create(
        activity_id=activity_id,
        conversation_id=conversation_id,
        kind=kind,
    )


def test_session_index_includes_active_activity_count_for_pending_and_running(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _upsert_index_row(db)
    _create_activity(db, "pending")
    _create_activity(db, "running", kind="team_dispatch")
    _create_activity(db, "other-conv", conversation_id="conversation-2")
    db.activities.update_status("running", "running")
    db.activities.update_status("other-conv", "running")

    item = _session_item(db)

    assert item["active_activity_count"] == 2
    assert item["unread_completion_count"] == 0


def test_session_index_includes_unread_completion_count_for_completed_failed_with_read_null(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _upsert_index_row(db)
    _create_activity(db, "completed")
    _create_activity(db, "failed", kind="team_dispatch")
    db.activities.mark_completed("completed", result_summary="done", result_json={})
    db.activities.mark_failed("failed", error_message="failed")

    item = _session_item(db)

    assert item["active_activity_count"] == 0
    assert item["unread_completion_count"] == 2


def test_session_index_excludes_read_completions(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _upsert_index_row(db)
    _create_activity(db, "unread")
    _create_activity(db, "read", kind="team_dispatch")
    db.activities.mark_completed("unread", result_summary="done", result_json={})
    db.activities.mark_failed("read", error_message="failed")
    db.activities.mark_read("read")

    item = _session_item(db)

    assert item["unread_completion_count"] == 1


def test_session_index_returns_zero_when_no_activities(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _upsert_index_row(db)

    item = _session_item(db)

    assert item["active_activity_count"] == 0
    assert item["unread_completion_count"] == 0


def test_session_index_aggregates_across_multiple_activities_per_conv(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _upsert_index_row(db)
    _create_activity(db, "pending")
    _create_activity(db, "running", kind="team_dispatch")
    _create_activity(db, "completed")
    _create_activity(db, "failed", kind="team_dispatch")
    _create_activity(db, "read", kind="member_chat")
    _create_activity(db, "cancelled", kind="member_chat")
    db.activities.update_status("running", "running")
    db.activities.mark_completed("completed", result_summary="done", result_json={})
    db.activities.mark_failed("failed", error_message="failed")
    db.activities.mark_completed("read", result_summary="done", result_json={})
    db.activities.mark_read("read")
    db.activities.mark_cancelled("cancelled")

    item = _session_item(db)

    assert item["active_activity_count"] == 2
    assert item["unread_completion_count"] == 2


def test_session_index_activity_join_falls_back_to_conversation_session_id(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _upsert_index_row(db, session_id="plain-session", conversation_id="")
    _create_activity(db, "pending", conversation_id="plain-session")

    item = _session_item(db, session_id="plain-session")

    assert item["active_activity_count"] == 1
    assert item["unread_completion_count"] == 0


def test_session_index_gateway_list_emits_activity_counts(monkeypatch, tmp_path: Path) -> None:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = _db(tmp_path)
    _upsert_index_row(db)
    _create_activity(db, "running")
    _create_activity(db, "completed", kind="team_dispatch")
    db.activities.update_status("running", "running")
    db.activities.mark_completed("completed", result_summary="done", result_json={})
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)

    response = server._methods["session.index.list"](1, {})

    assert "error" not in response
    [item] = response["result"]["sessions"]
    assert item["active_activity_count"] == 1
    assert item["unread_completion_count"] == 1
