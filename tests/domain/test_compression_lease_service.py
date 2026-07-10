from __future__ import annotations

from hermes_agent.domain.compression_lease_service import CompressionLeaseService
from hermes_agent.repositories.compression_lease_repo import CompressionLeaseRepository
from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


def _service(conn, clock):
    return CompressionLeaseService(
        CompressionLeaseRepository(conn),
        SqliteUnitOfWork(conn, lock_for_connection(conn)),
        clock=clock,
    )


def test_compression_lease_is_reentrant_and_owner_released(tmp_path):
    now = [100.0]
    conn = connect_session_repository_db(tmp_path / "state.db")
    SessionRepoImpl(conn).create(SessionSpec(session_id="session-1", source="cli"))
    leases = _service(conn, lambda: now[0])
    try:
        assert leases.try_acquire("session-1", "foreground", ttl_seconds=30) is True
        assert leases.try_acquire("session-1", "background", ttl_seconds=30) is False
        assert leases.try_acquire("session-1", "foreground", ttl_seconds=60) is True
        assert leases.holder("session-1") == "foreground"

        assert leases.release("session-1", "background") is False
        assert leases.release("session-1", "foreground") is True
        assert leases.holder("session-1") is None
    finally:
        conn.close()


def test_expired_compression_lease_can_be_claimed_by_another_holder(tmp_path):
    now = [100.0]
    conn = connect_session_repository_db(tmp_path / "state.db")
    SessionRepoImpl(conn).create(SessionSpec(session_id="session-1", source="cli"))
    leases = _service(conn, lambda: now[0])
    try:
        assert leases.try_acquire("session-1", "foreground", ttl_seconds=10) is True
        now[0] = 111.0

        assert leases.holder("session-1") is None
        assert leases.try_acquire("session-1", "background", ttl_seconds=10) is True
        assert leases.holder("session-1") == "background"
    finally:
        conn.close()


def test_compression_lease_excludes_independent_database_connections(tmp_path):
    db_path = tmp_path / "state.db"
    conn_a = connect_session_repository_db(db_path)
    SessionRepoImpl(conn_a).create(SessionSpec(session_id="session-1", source="cli"))
    conn_b = connect_session_repository_db(db_path)
    leases_a = _service(conn_a, lambda: 100.0)
    leases_b = _service(conn_b, lambda: 100.0)
    try:
        assert leases_a.try_acquire("session-1", "process-a", ttl_seconds=30) is True
        assert leases_b.try_acquire("session-1", "process-b", ttl_seconds=30) is False
        assert leases_b.holder("session-1") == "process-a"

        assert leases_a.release("session-1", "process-a") is True
        assert leases_b.try_acquire("session-1", "process-b", ttl_seconds=30) is True
    finally:
        conn_b.close()
        conn_a.close()


def test_compression_lease_is_deleted_with_owning_session(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    SessionRepoImpl(conn).create(SessionSpec(session_id="session-1", source="cli"))
    leases = _service(conn, lambda: 100.0)
    try:
        assert leases.try_acquire("session-1", "foreground") is True
        conn.execute("DELETE FROM sessions WHERE id = ?", ("session-1",))

        row = conn.execute(
            "SELECT 1 FROM session_compression_leases WHERE session_id = ?",
            ("session-1",),
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_compression_lease_rejects_invalid_identity_and_ttl(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    SessionRepoImpl(conn).create(SessionSpec(session_id="session-1", source="cli"))
    leases = _service(conn, lambda: 100.0)
    try:
        assert leases.try_acquire("", "foreground") is False
        assert leases.try_acquire("session-1", "") is False

        try:
            leases.try_acquire("session-1", "foreground", ttl_seconds=0)
        except ValueError as exc:
            assert "ttl_seconds" in str(exc)
        else:
            raise AssertionError("zero compression lease TTL must be rejected")
    finally:
        conn.close()
