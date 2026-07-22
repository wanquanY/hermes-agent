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

    def conversation_session_id(self, mission_id: str) -> str:
        """Return the routable Conversation session that owns a Team Mission.

        Runtime and journal sessions are execution identities.  Consumers
        projecting mission events into Conversation-owned UI surfaces must use
        the canonical Conversation mapping instead of inferring ownership from
        an individual event subject.  ``team_missions.conversation_id`` is the
        aggregate id; ``team_mission_conversations.conversation_session_id`` is
        the transport identity and may differ for migrated conversations.
        """
        stable = str(mission_id or "").strip()
        if not stable:
            return ""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(
                           NULLIF(conversation.conversation_session_id, ''),
                           mission.conversation_id
                       ) AS conversation_session_id
                  FROM team_missions mission
                  LEFT JOIN team_mission_conversations conversation
                    ON conversation.conversation_id = mission.conversation_id
                 WHERE mission.mission_id = ?
                """,
                (stable,),
            ).fetchone()
        return str(row["conversation_session_id"] or "").strip() if row is not None else ""

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

    def mission_ids_for_session_key(self, session_key: str) -> list[str]:
        stable_session_key = str(session_key or "").strip()
        if not stable_session_key:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT tm.mission_id
                  FROM team_missions tm
                  JOIN team_mission_conversations tmc
                    ON tmc.conversation_id = tm.conversation_id
                 WHERE tmc.conversation_session_id = ?
                UNION
                SELECT DISTINCT mission_id
                  FROM team_mission_run_bindings
                 WHERE session_id = ?
                    OR execution_session_id = ?
                    OR runtime_scope_key = ?
                """,
                (
                    stable_session_key,
                    stable_session_key,
                    stable_session_key,
                    stable_session_key,
                ),
            ).fetchall()
        return [
            mission_id
            for mission_id in (
                str(row["mission_id"] or "").strip()
                for row in rows
            )
            if mission_id
        ]


__all__ = ["TeamMissionReadModel"]
