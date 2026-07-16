"""Repository owner for the session runtime stability aggregate."""

from __future__ import annotations

from hermes_agent.domain.runtime_stability import SessionRuntimeStability
from hermes_agent.repositories.base import RepositoryConnection


class RuntimeStabilityRepository:
    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def get(self, session_id: str) -> SessionRuntimeStability | None:
        row = self._conn.execute(
            """
            SELECT session_id,
                   compression_ineffective_count,
                   compression_fallback_streak,
                   compression_verdict_pending,
                   compression_failure_cooldown_until,
                   compression_failure_error,
                   stream_stale_failures,
                   stream_stale_retry_after,
                   stream_stale_route_hash,
                   stream_stale_last_error,
                   updated_at
            FROM session_runtime_stability
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionRuntimeStability(
            session_id=str(row["session_id"] or ""),
            compression_ineffective_count=int(
                row["compression_ineffective_count"] or 0
            ),
            compression_fallback_streak=int(
                row["compression_fallback_streak"] or 0
            ),
            compression_verdict_pending=bool(row["compression_verdict_pending"]),
            compression_failure_cooldown_until=float(
                row["compression_failure_cooldown_until"] or 0
            ),
            compression_failure_error=str(
                row["compression_failure_error"] or ""
            ),
            stream_stale_failures=int(row["stream_stale_failures"] or 0),
            stream_stale_retry_after=float(row["stream_stale_retry_after"] or 0),
            stream_stale_route_hash=str(row["stream_stale_route_hash"] or ""),
            stream_stale_last_error=str(row["stream_stale_last_error"] or ""),
            updated_at=float(row["updated_at"] or 0),
        )

    def write_compression(
        self,
        session_id: str,
        *,
        ineffective_count: int,
        fallback_streak: int,
        verdict_pending: bool,
        cooldown_until: float,
        error: str,
        updated_at: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO session_runtime_stability (
                session_id,
                compression_ineffective_count,
                compression_fallback_streak,
                compression_verdict_pending,
                compression_failure_cooldown_until,
                compression_failure_error,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                compression_ineffective_count = excluded.compression_ineffective_count,
                compression_fallback_streak = excluded.compression_fallback_streak,
                compression_verdict_pending = excluded.compression_verdict_pending,
                compression_failure_cooldown_until = excluded.compression_failure_cooldown_until,
                compression_failure_error = excluded.compression_failure_error,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                ineffective_count,
                fallback_streak,
                int(verdict_pending),
                cooldown_until,
                error,
                updated_at,
            ),
        )

    def record_stream_stale_failure(
        self,
        session_id: str,
        *,
        route_hash: str,
        threshold: int,
        retry_after: float,
        error: str,
        updated_at: float,
    ) -> SessionRuntimeStability:
        self._conn.execute(
            """
            INSERT INTO session_runtime_stability (
                session_id,
                stream_stale_failures,
                stream_stale_retry_after,
                stream_stale_route_hash,
                stream_stale_last_error,
                updated_at
            ) VALUES (?, 1, CASE WHEN 1 >= ? THEN ? ELSE 0 END, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                stream_stale_failures = CASE
                    WHEN session_runtime_stability.stream_stale_route_hash = excluded.stream_stale_route_hash
                    THEN session_runtime_stability.stream_stale_failures + 1
                    ELSE 1
                END,
                stream_stale_retry_after = CASE
                    WHEN (
                        CASE
                            WHEN session_runtime_stability.stream_stale_route_hash = excluded.stream_stale_route_hash
                            THEN session_runtime_stability.stream_stale_failures + 1
                            ELSE 1
                        END
                    ) >= ? THEN ? ELSE 0 END,
                stream_stale_route_hash = excluded.stream_stale_route_hash,
                stream_stale_last_error = excluded.stream_stale_last_error,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                threshold,
                retry_after,
                route_hash,
                error,
                updated_at,
                threshold,
                retry_after,
            ),
        )
        state = self.get(session_id)
        if state is None:  # pragma: no cover - INSERT/UPDATE invariant
            raise RuntimeError("stream stale state write did not materialize")
        return state

    def clear_stream_stale(self, session_id: str, *, updated_at: float) -> None:
        self._conn.execute(
            """
            INSERT INTO session_runtime_stability (session_id, updated_at)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                stream_stale_failures = 0,
                stream_stale_retry_after = 0,
                stream_stale_route_hash = '',
                stream_stale_last_error = '',
                updated_at = excluded.updated_at
            """,
            (session_id, updated_at),
        )

    def carry_forward(
        self,
        old_session_id: str,
        new_session_id: str,
        *,
        updated_at: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO session_runtime_stability (
                session_id,
                compression_ineffective_count,
                compression_fallback_streak,
                compression_verdict_pending,
                compression_failure_cooldown_until,
                compression_failure_error,
                stream_stale_failures,
                stream_stale_retry_after,
                stream_stale_route_hash,
                stream_stale_last_error,
                updated_at
            )
            SELECT ?,
                   compression_ineffective_count,
                   compression_fallback_streak,
                   compression_verdict_pending,
                   compression_failure_cooldown_until,
                   compression_failure_error,
                   stream_stale_failures,
                   stream_stale_retry_after,
                   stream_stale_route_hash,
                   stream_stale_last_error,
                   ?
            FROM session_runtime_stability
            WHERE session_id = ?
            ON CONFLICT(session_id) DO UPDATE SET
                compression_ineffective_count = excluded.compression_ineffective_count,
                compression_fallback_streak = excluded.compression_fallback_streak,
                compression_verdict_pending = excluded.compression_verdict_pending,
                compression_failure_cooldown_until = excluded.compression_failure_cooldown_until,
                compression_failure_error = excluded.compression_failure_error,
                stream_stale_failures = excluded.stream_stale_failures,
                stream_stale_retry_after = excluded.stream_stale_retry_after,
                stream_stale_route_hash = excluded.stream_stale_route_hash,
                stream_stale_last_error = excluded.stream_stale_last_error,
                updated_at = excluded.updated_at
            """,
            (new_session_id, updated_at, old_session_id),
        )


__all__ = ["RuntimeStabilityRepository"]
