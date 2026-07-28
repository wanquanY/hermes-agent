"""Bounded read model for cron execution session history."""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


class CronRunHistoryReadModel:
    """Project one cron job's run sessions without scanning global history.

    Cron execution sessions use ``cron_{job_id}_{timestamp}`` identifiers.  A
    half-open prefix range plus the composite ``(source, id)`` index keeps the
    query proportional to the requested job and page instead of all cron runs.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)

    def list(
        self,
        job_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        canonical = str(job_id or "").strip()
        if not canonical:
            return []
        bounded_limit = max(1, min(_to_int(limit, 20), 100))
        bounded_offset = max(0, _to_int(offset, 0))
        prefix = f"cron_{canonical}_"
        prefix_hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.*,
                       COALESCE(s.preview, '') AS _cron_preview,
                       COALESCE(s.last_active, s.started_at) AS _cron_last_active
                  FROM sessions s INDEXED BY idx_sessions_source_id
                 WHERE s.source = 'cron' AND s.id >= ? AND s.id < ?
                 ORDER BY s.started_at DESC, s.id DESC
                 LIMIT ? OFFSET ?
                """,
                (prefix, prefix_hi, bounded_limit, bounded_offset),
            ).fetchall()
        return [_project_row(row) for row in rows]


def _project_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["preview"] = str(result.pop("_cron_preview", "") or "")
    result["last_active"] = float(
        result.pop("_cron_last_active", result.get("started_at") or 0) or 0
    )
    return result


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["CronRunHistoryReadModel"]
