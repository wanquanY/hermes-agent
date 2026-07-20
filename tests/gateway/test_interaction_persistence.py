from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services.interaction_registry import pending_interactions
from tui_gateway.services.interaction_registry import persist_interaction_event
from tui_gateway.services import run_control
from hermes_agent.orchestration.worker_frame_router import PendingEntry


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
    db = open_cli_session_store(db_path)
    db.sessions.create("conversation-session-1", "hermes")

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
    db = open_cli_session_store(db_path)
    db.sessions.create("conversation-session-1", "hermes")

    requested = persist_interaction_event(db, "interaction.requested", _entry("req-1"))

    db.close()

    rows = _rows(db_path)
    assert requested["type"] == "_internal.interaction.requested"
    assert rows[0]["seq"] == 1
    assert rows[0]["anchor_seq"] == 0


def test_default_run_events_list_filters_internal_interactions(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")
    db.runs.append_event(
        "conversation-session-1",
        {
            "type": "message.start",
            "run_id": "run-1",
            "payload": {"text": "hello"},
        },
    )
    persist_interaction_event(db, "interaction.requested", _entry("req-1"))

    default_events = db.runs.list_events("conversation-session-1")
    internal_events = db.runs.list_events("conversation-session-1", include_internal=True)

    assert [event["type"] for event in default_events] == ["message.start"]
    assert [event["type"] for event in internal_events] == [
        "message.start",
        "_internal.interaction.requested",
    ]


def test_pending_interactions_recovery_excludes_resolved_and_expired(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")

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


def test_pending_interaction_replays_after_cursor_with_render_identity_and_no_secret_values(
    tmp_path: Path,
) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")
    db.runs.upsert(
        run_id="run-waiting-1",
        session_id="conversation-session-1",
        runtime_scope_key="profile:agent-default",
        execution_session_id="runtime-session-1",
        status="running",
        metadata={"gateway_pid": os.getpid()},
    )
    entry = PendingEntry(
        request_id="req-render-ready",
        kind="clarify",
        conversation_id="runtime-session-1",
        session_key="conversation-session-1",
        scope_key="profile:agent-default",
        state="pending",
        request_payload={
            "question": "Choose an environment",
            "choices": ["staging", "production", "staging"],
            "run_id": "run-waiting-1",
            "turn_id": "turn-waiting-1",
            "participant_id": "agent:profile-default",
            "activity_id": "chat:conversation-session-1",
            "activity_kind": "chat",
            "runtime_scope_key": "profile:agent-default",
            "value": "must-not-persist",
            "password": "must-not-persist",
            "command": "must-not-persist",
            "metadata": {"token": "must-not-persist"},
        },
    )
    saved = persist_interaction_event(db, "interaction.requested", entry)

    recovered = pending_interactions(db, "conversation-session-1")
    assert recovered[0]["request"] == {
        "question": "Choose an environment",
        "choices": ["staging", "production"],
        "run_id": "run-waiting-1",
        "turn_id": "turn-waiting-1",
        "participant_id": "agent:profile-default",
        "activity_id": "chat:conversation-session-1",
        "activity_kind": "chat",
        "runtime_scope_key": "profile:agent-default",
    }

    _subscription_id, replay = run_control.subscribe_session_with_id(
        conversation_session_id="conversation-session-1",
        transport=None,
        after_seq=int(saved["seq"]),
        db=db,
    )

    assert len(replay) == 1
    assert replay[0] == {
        "type": "interaction.requested",
        "conversation_session_id": "conversation-session-1",
        "session_id": "conversation-session-1",
        "run_id": "run-waiting-1",
        "turn_id": "turn-waiting-1",
        "participant_id": "agent:profile-default",
        "runtime_scope_key": "profile:agent-default",
        "transient": True,
        "source_seq": int(saved["seq"]),
        "runtime_source_seq": int(saved["seq"]),
        "payload": {
            "question": "Choose an environment",
            "choices": ["staging", "production"],
            "run_id": "run-waiting-1",
            "turn_id": "turn-waiting-1",
            "participant_id": "agent:profile-default",
            "activity_id": "chat:conversation-session-1",
            "activity_kind": "chat",
            "runtime_scope_key": "profile:agent-default",
            "request_id": "req-render-ready",
            "kind": "clarify",
            "status": "pending",
            "source_event_type": "clarify.request",
            "replay_snapshot": True,
        },
    }
    db.close()


def test_pending_interaction_replay_excludes_terminal_owner_run(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")
    db.runs.upsert(
        run_id="run-completed-1",
        session_id="conversation-session-1",
        runtime_scope_key="profile:agent-default",
        execution_session_id="runtime-session-1",
        status="completed",
    )
    saved = persist_interaction_event(
        db,
        "interaction.requested",
        PendingEntry(
            request_id="req-stale",
            kind="approval",
            conversation_id="runtime-session-1",
            session_key="conversation-session-1",
            scope_key="profile:agent-default",
            state="pending",
            request_payload={
                "run_id": "run-completed-1",
                "turn_id": "turn-completed-1",
                "runtime_scope_key": "profile:agent-default",
            },
        ),
    )

    _subscription_id, replay = run_control.subscribe_session_with_id(
        conversation_session_id="conversation-session-1",
        transport=None,
        after_seq=int(saved["seq"]),
        db=db,
    )

    assert replay == []
    db.close()


def test_subscribe_recovers_restarted_owner_before_interaction_replay(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")
    stale_time = time.time() - 301
    db.runs.upsert(
        run_id="run-abandoned-1",
        session_id="conversation-session-1",
        runtime_scope_key="profile:agent-default",
        execution_session_id="runtime-session-1",
        status="running",
        started_at=stale_time,
        updated_at=stale_time,
        metadata={
            "gateway_pid": os.getpid(),
            "gateway_instance_id": "previous-gateway",
        },
    )
    saved = persist_interaction_event(
        db,
        "interaction.requested",
        PendingEntry(
            request_id="req-abandoned",
            kind="clarify",
            conversation_id="runtime-session-1",
            session_key="conversation-session-1",
            scope_key="profile:agent-default",
            state="pending",
            request_payload={
                "question": "This caller no longer exists",
                "run_id": "run-abandoned-1",
                "turn_id": "turn-abandoned-1",
                "runtime_scope_key": "profile:agent-default",
            },
        ),
    )

    _subscription_id, replay = run_control.subscribe_session_with_id(
        conversation_session_id="conversation-session-1",
        transport=None,
        after_seq=int(saved["seq"]),
        db=db,
        current_gateway_instance_id="current-gateway",
    )

    assert db.runs.get("run-abandoned-1")["status"] == "failed"
    assert all(event.get("type") != "interaction.requested" for event in replay)
    db.close()


def test_pending_interactions_recovery_uses_run_component(tmp_path: Path, monkeypatch) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")
    persist_interaction_event(db, "interaction.requested", _entry("req-pending", anchor_seq=42))

    original = db.runs.list_events
    calls: list[tuple[str, bool]] = []

    def traced(session_id, *, include_internal=False, **options):
        calls.append((session_id, include_internal))
        return original(
            session_id,
            include_internal=include_internal,
            **options,
        )

    monkeypatch.setattr(db.runs, "list_events", traced)

    assert pending_interactions(db, "conversation-session-1") == [
        {
            "request_id": "req-pending",
            "kind": "approval",
            "status": "pending",
            "anchor_seq": 42,
            "seq": 1,
        }
    ]
    assert calls == [("conversation-session-1", True)]


def test_interaction_lifecycle_requires_run_component() -> None:
    with pytest.raises(RuntimeError, match="run component"):
        persist_interaction_event(None, "interaction.requested", _entry("req-1"))


def test_interaction_lifecycle_requires_conversation_session_id() -> None:
    class _DB:
        pass

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
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")
    with pytest.raises(ValueError, match="request_id"):
        persist_interaction_event(db, "interaction.requested", _entry(""))


def test_non_interaction_event_type_is_noop(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-session-1", "hermes")
    assert persist_interaction_event(db, "message.complete", _entry("req-1")) == {}
    assert db.runs.list_events("conversation-session-1", include_internal=True) == []
