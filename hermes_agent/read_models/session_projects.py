"""Read projections used by first-class project discovery and grouping."""

from __future__ import annotations

import sqlite3
from typing import Any


class SessionProjectReadModel:
    """Project-oriented session reads kept out of CRUD repositories and RPCs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def distinct_cwds(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT cwd,
                   COUNT(*) AS sessions,
                   MAX(COALESCE(last_active, started_at, 0)) AS last_active
              FROM sessions
             WHERE cwd IS NOT NULL
               AND TRIM(cwd) != ''
               AND COALESCE(session_kind, 'hermes_session') != 'execution'
               AND COALESCE(conversation_kind, 'direct') IN ('direct', 'team')
             GROUP BY cwd
             ORDER BY last_active DESC, cwd ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["SessionProjectReadModel"]
