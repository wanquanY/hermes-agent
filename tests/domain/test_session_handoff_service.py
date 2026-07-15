from __future__ import annotations

from hermes_agent.domain.message_service import MessageService
from hermes_agent.domain.session_service import SessionService
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


def _service(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    repo = SessionRepoImpl(conn)
    unit_of_work = SqliteUnitOfWork(conn, lock_for_connection(conn))
    service = SessionService(
        conn,
        repo,
        SessionRecallReadModel(conn),
        MessageService(conn, repo),
        unit_of_work,
    )
    return conn, service


def test_handoff_state_machine_runs_through_session_service(tmp_path):
    conn, sessions = _service(tmp_path)
    try:
        sessions.create("session-1", source="cli")

        assert sessions.request_handoff("session-1", "telegram") is True
        assert sessions.request_handoff("session-1", "discord") is False
        assert [row["id"] for row in sessions.list_pending_handoffs()] == ["session-1"]

        assert sessions.claim_handoff("session-1") is True
        assert sessions.claim_handoff("session-1") is False
        assert sessions.handoff_state("session-1") == {
            "state": "running",
            "platform": "telegram",
            "error": None,
        }

        sessions.complete_handoff("session-1")
        assert sessions.handoff_state("session-1")["state"] == "completed"
        assert sessions.list_pending_handoffs() == []
    finally:
        conn.close()


def test_handoff_retry_clears_error_and_failure_reason_is_bounded(tmp_path):
    conn, sessions = _service(tmp_path)
    try:
        sessions.create("session-1", source="cli")
        sessions.request_handoff("session-1", "telegram")
        sessions.claim_handoff("session-1")
        sessions.fail_handoff("session-1", "x" * 1000)

        failed = sessions.handoff_state("session-1")
        assert failed["state"] == "failed"
        assert len(failed["error"]) == 500

        assert sessions.request_handoff("session-1", "discord") is True
        retried = sessions.handoff_state("session-1")
        assert retried == {
            "state": "pending",
            "platform": "discord",
            "error": None,
        }
    finally:
        conn.close()


def test_pending_handoffs_are_ordered_by_request_time(tmp_path):
    conn, sessions = _service(tmp_path)
    try:
        sessions.create("session-b", source="cli")
        sessions.create("session-a", source="cli")
        sessions.request_handoff("session-b", "discord")
        sessions.request_handoff("session-a", "telegram")

        rows = sessions.list_pending_handoffs()

        assert [row["id"] for row in rows] == ["session-b", "session-a"]
    finally:
        conn.close()
