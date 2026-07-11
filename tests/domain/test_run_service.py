from __future__ import annotations

from hermes_agent.domain.run_service import RunService
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


def test_session_info_projection_crosses_run_to_session_boundary(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    try:
        sessions = SessionRepoImpl(conn)
        service = RunService(
            conn,
            SqliteUnitOfWork(conn, lock_for_connection(conn)),
            sessions,
        )

        saved = service.append_event(
            "conversation-1",
            {
                "type": "session.info",
                "execution_session_id": "exec-1",
                "runtime_scope_key": "profile:agent-default",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "timestamp": 20.0,
                "payload": {
                    "status": "running",
                    "model": "test-model",
                    "provider": "test-provider",
                },
            },
        )

        row = conn.execute(
            "SELECT * FROM session_runtime_state WHERE session_id = ?",
            ("conversation-1",),
        ).fetchone()
        state = service.runtime_state("conversation-1")
        assert row["source_seq"] == saved["seq"]
        assert row["execution_session_id"] == "exec-1"
        assert row["run_id"] == "run-1"
        assert state["source_seq"] == saved["seq"]
        assert state["execution_session_id"] == "exec-1"
        assert state["run_id"] == "run-1"
    finally:
        conn.close()
