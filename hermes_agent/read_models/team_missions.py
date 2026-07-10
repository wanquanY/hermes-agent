"""Read model for Team Mission lifecycle state."""

from __future__ import annotations

import sqlite3

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


__all__ = ["TeamMissionReadModel"]
