from __future__ import annotations

from hermes_agent.application.runtime_stability_service import (
    SessionRuntimeStabilityService,
)
from hermes_agent.composition.session_repository_db import (
    connect_session_repository_db,
)
from hermes_agent.repositories.runtime_stability_repo import (
    RuntimeStabilityRepository,
)
from hermes_agent.repositories.session_repo import SessionRepoImpl, SessionSpec
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


def _service(conn, clock):
    return SessionRuntimeStabilityService(
        RuntimeStabilityRepository(conn),
        SqliteUnitOfWork(conn, lock_for_connection(conn)),
        clock=clock,
    )


def test_runtime_stability_round_trips_compression_and_carries_to_child(tmp_path):
    now = [100.0]
    conn = connect_session_repository_db(tmp_path / "state.db")
    sessions = SessionRepoImpl(conn)
    sessions.create(SessionSpec(session_id="parent", source="cli"))
    sessions.create(
        SessionSpec(
            session_id="child",
            source="cli",
            parent_session_id="parent",
        )
    )
    stability = _service(conn, lambda: now[0])
    try:
        stability.write_compression(
            "parent",
            ineffective_count=2,
            fallback_streak=1,
            verdict_pending=True,
            cooldown_until=130.0,
            error="provider unavailable",
        )

        parent = stability.get("parent")
        assert parent.compression_ineffective_count == 2
        assert parent.compression_fallback_streak == 1
        assert parent.compression_verdict_pending is True
        assert parent.compression_failure_cooldown_until == 130.0
        assert parent.compression_failure_error == "provider unavailable"

        now[0] = 101.0
        stability.carry_forward("parent", "child")
        child = stability.get("child")
        assert child.compression_ineffective_count == 2
        assert child.compression_fallback_streak == 1
        assert child.compression_verdict_pending is True
        assert child.compression_failure_cooldown_until == 130.0
        assert child.updated_at == 101.0
    finally:
        conn.close()


def test_stream_stale_failure_is_atomic_route_scoped_and_resettable(tmp_path):
    now = [200.0]
    conn = connect_session_repository_db(tmp_path / "state.db")
    SessionRepoImpl(conn).create(SessionSpec(session_id="session-1", source="cli"))
    stability = _service(conn, lambda: now[0])
    try:
        first = stability.record_stream_stale_failure(
            "session-1",
            route_hash="route-a",
            threshold=2,
            open_seconds=30,
            error="first stale response",
        )
        assert first.stream_stale_failures == 1
        assert first.stream_stale_retry_after == 0

        second = stability.record_stream_stale_failure(
            "session-1",
            route_hash="route-a",
            threshold=2,
            open_seconds=30,
            error="second stale response",
        )
        assert second.stream_stale_failures == 2
        assert second.stream_stale_retry_after == 230.0
        assert second.stream_stale_last_error == "second stale response"

        # A different provider/model route starts a fresh streak rather than
        # inheriting an open circuit from the previous route.
        changed = stability.record_stream_stale_failure(
            "session-1",
            route_hash="route-b",
            threshold=2,
            open_seconds=30,
            error="new route stale response",
        )
        assert changed.stream_stale_failures == 1
        assert changed.stream_stale_retry_after == 0
        assert changed.stream_stale_route_hash == "route-b"

        stability.clear_stream_stale("session-1")
        cleared = stability.get("session-1")
        assert cleared.stream_stale_failures == 0
        assert cleared.stream_stale_retry_after == 0
        assert cleared.stream_stale_route_hash == ""
        assert cleared.stream_stale_last_error == ""
    finally:
        conn.close()


def test_runtime_stability_is_deleted_with_owning_session(tmp_path):
    conn = connect_session_repository_db(tmp_path / "state.db")
    SessionRepoImpl(conn).create(SessionSpec(session_id="session-1", source="cli"))
    stability = _service(conn, lambda: 100.0)
    try:
        stability.write_compression(
            "session-1",
            ineffective_count=1,
            fallback_streak=0,
            verdict_pending=False,
        )
        conn.execute("DELETE FROM sessions WHERE id = ?", ("session-1",))

        row = conn.execute(
            "SELECT 1 FROM session_runtime_stability WHERE session_id = ?",
            ("session-1",),
        ).fetchone()
        assert row is None
    finally:
        conn.close()
