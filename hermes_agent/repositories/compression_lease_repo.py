"""SQLite repository for cross-process session compression leases."""

from __future__ import annotations

from dataclasses import dataclass

from hermes_agent.repositories.base import RepositoryConnection


@dataclass(frozen=True)
class CompressionLease:
    session_id: str
    holder: str
    expires_at: float
    updated_at: float


class CompressionLeaseRepository:
    """Owns the ``session_compression_leases`` table."""

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def try_acquire(
        self,
        session_id: str,
        holder: str,
        *,
        now: float,
        expires_at: float,
    ) -> bool:
        cursor = self._conn.execute(
            """
            INSERT INTO session_compression_leases (
                session_id, holder, expires_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                holder = excluded.holder,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            WHERE session_compression_leases.expires_at <= ?
               OR session_compression_leases.holder = excluded.holder
            """,
            (session_id, holder, expires_at, now, now),
        )
        return int(cursor.rowcount or 0) > 0

    def release(self, session_id: str, holder: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM session_compression_leases WHERE session_id = ? AND holder = ?",
            (session_id, holder),
        )
        return int(cursor.rowcount or 0) > 0

    def holder(self, session_id: str, *, now: float) -> str | None:
        self._conn.execute(
            "DELETE FROM session_compression_leases WHERE session_id = ? AND expires_at <= ?",
            (session_id, now),
        )
        row = self._conn.execute(
            "SELECT holder FROM session_compression_leases WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return str(row["holder"] or "") if row else None

    def get(self, session_id: str) -> CompressionLease | None:
        row = self._conn.execute(
            "SELECT session_id, holder, expires_at, updated_at "
            "FROM session_compression_leases WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return CompressionLease(
            session_id=str(row["session_id"] or ""),
            holder=str(row["holder"] or ""),
            expires_at=float(row["expires_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )


__all__ = ["CompressionLease", "CompressionLeaseRepository"]
