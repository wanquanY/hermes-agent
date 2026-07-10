"""Read model for Team Mission lifecycle state."""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


class TeamMissionReadModel:
    """Owns query-only access to the Team Mission aggregate."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)

    def exists(self, mission_id: str) -> bool:
        stable = str(mission_id or "").strip()
        if not stable:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM team_missions WHERE mission_id = ? LIMIT 1",
                (stable,),
            ).fetchone()
        return row is not None

    def status(self, mission_id: str) -> str:
        stable = str(mission_id or "").strip()
        if not stable:
            return ""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM team_missions WHERE mission_id = ?",
                (stable,),
            ).fetchone()
        return str(row["status"] or "").strip().lower() if row is not None else ""

    def mission_id_for_run(self, run_id: str) -> str:
        stable = str(run_id or "").strip()
        if not stable:
            return ""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mission_id
                  FROM team_mission_run_bindings
                 WHERE run_id = ?
                """,
                (stable,),
            ).fetchone()
        return str(row["mission_id"] or "").strip() if row is not None else ""

    def list_node_run_attempts(
        self,
        mission_id: str,
        node_id: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        stable_mission_id = str(mission_id or "").strip()
        stable_node_id = str(node_id or "").strip()
        if not stable_mission_id or not stable_node_id:
            return []
        bounded_limit = max(1, min(int(limit or 5), 100))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT b.run_id,
                       b.session_id,
                       b.execution_session_id,
                       b.role,
                       b.created_at,
                       r.status AS run_status,
                       r.updated_at AS run_updated_at
                  FROM team_mission_run_bindings b
                  LEFT JOIN runs r ON r.run_id = b.run_id
                 WHERE b.mission_id = ? AND b.node_id = ?
                 ORDER BY b.created_at DESC, b.run_id DESC
                 LIMIT ?
                """,
                (stable_mission_id, stable_node_id, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["TeamMissionReadModel"]
