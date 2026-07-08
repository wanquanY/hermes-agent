from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tui_gateway.services.interaction_registry import pending_interactions
from tui_gateway.services.interaction_registry import persist_interaction_event
from tui_gateway.services.worker_frame_router import PendingEntry


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT event_type, interaction_request_id, interaction_kind,
                   interaction_status, anchor_seq, seq
              FROM run_events
             ORDER BY seq ASC
            """
        ).fetchall()
    finally:
        conn.close()


def _entry(
    request_id: str,
    state: str = "pending",
    choice: object = None,
    *,
    anchor_seq: int = 0,
) -> PendingEntry:
    return PendingEntry(
        request_id=request_id,
        kind="approval",
        conversation_id="runtime-session-1",
        session_key="conversation-session-1",
        scope_key="member-chat:conversation-session-1:m1",
        state=state,
        choice=choice,
        anchor_seq=anchor_seq,
    )


def test_interaction_lifecycle_persists_as_internal_events(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("conversation-session-1", "hermes")

    requested = persist_interaction_event(
        db,
        "interaction.requested",
        _entry("req-1", anchor_seq=7),
    )
    resolved_entry = _entry("req-1", state="resolved", choice={"allow": True})
    resolved = persist_interaction_event(db, "interaction.resolved", resolved_entry)

    db.close()

    assert requested["type"] == "_internal.interaction.requested"
    assert resolved["type"] == "_internal.interaction.resolved"

    rows = _rows(db_path)
    assert [row["event_type"] for row in rows] == [
        "_internal.interaction.requested",
        "_internal.interaction.resolved",
    ]
    assert rows[0]["interaction_request_id"] == "req-1"
    assert rows[0]["interaction_kind"] == "approval"
    assert rows[0]["interaction_status"] == "pending"
    assert rows[0]["anchor_seq"] == 7
    assert rows[1]["interaction_status"] == "resolved"
    assert rows[1]["anchor_seq"] == 7


def test_interaction_request_does_not_self_anchor_without_caller_seq(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("conversation-session-1", "hermes")

    requested = persist_interaction_event(db, "interaction.requested", _entry("req-1"))

    db.close()

    rows = _rows(db_path)
    assert requested["type"] == "_internal.interaction.requested"
    assert rows[0]["seq"] == 1
    assert rows[0]["anchor_seq"] == 0


def test_default_run_events_list_filters_internal_interactions(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conversation-session-1", "hermes")
    db.append_run_event(
        "conversation-session-1",
        {
            "type": "message.start",
            "run_id": "run-1",
            "payload": {"text": "hello"},
        },
    )
    persist_interaction_event(db, "interaction.requested", _entry("req-1"))

    default_events = db.list_run_events("conversation-session-1")
    internal_events = db.list_run_events("conversation-session-1", include_internal=True)

    assert [event["type"] for event in default_events] == ["message.start"]
    assert [event["type"] for event in internal_events] == [
        "message.start",
        "_internal.interaction.requested",
    ]


def test_pending_interactions_recovery_excludes_resolved_and_expired(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conversation-session-1", "hermes")

    persist_interaction_event(db, "interaction.requested", _entry("req-pending", anchor_seq=42))
    persist_interaction_event(db, "interaction.requested", _entry("req-resolved"))
    persist_interaction_event(db, "interaction.resolved", _entry("req-resolved", state="resolved"))
    persist_interaction_event(db, "interaction.requested", _entry("req-expired"))
    persist_interaction_event(db, "interaction.expired", _entry("req-expired", state="expired"))

    recovered = pending_interactions(db, "conversation-session-1")

    assert recovered == [
        {
            "request_id": "req-pending",
            "kind": "approval",
            "status": "pending",
            "anchor_seq": 42,
            "seq": 1,
        }
    ]


def test_pending_interactions_recovery_uses_run_event_read_model(tmp_path: Path, monkeypatch) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conversation-session-1", "hermes")
    persist_interaction_event(db, "interaction.requested", _entry("req-pending", anchor_seq=42))

    def fail_legacy_list(*args, **kwargs):
        raise AssertionError("legacy db.list_run_events must not be called")

    monkeypatch.setattr(db, "list_run_events", fail_legacy_list)

    assert pending_interactions(db, "conversation-session-1") == [
        {
            "request_id": "req-pending",
            "kind": "approval",
            "status": "pending",
            "anchor_seq": 42,
            "seq": 1,
        }
    ]


def test_interaction_lifecycle_requires_db_append_run_event() -> None:
    with pytest.raises(RuntimeError, match="append_run_event"):
        persist_interaction_event(None, "interaction.requested", _entry("req-1"))


def test_interaction_lifecycle_requires_stable_session_id() -> None:
    class _DB:
        def append_run_event(self, session_id, frame):
            return {"type": frame.get("type")}

    entry = PendingEntry(
        request_id="req-1",
        kind="approval",
        conversation_id="",
        session_key="",
        scope_key="",
    )
    with pytest.raises(ValueError, match="stable session id"):
        persist_interaction_event(_DB(), "interaction.requested", entry)


def test_interaction_lifecycle_requires_request_id(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conversation-session-1", "hermes")
    with pytest.raises(ValueError, match="request_id"):
        persist_interaction_event(db, "interaction.requested", _entry(""))


def test_non_interaction_event_type_is_noop(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conversation-session-1", "hermes")
    assert persist_interaction_event(db, "message.complete", _entry("req-1")) == {}
    assert db.list_run_events("conversation-session-1", include_internal=True) == []
