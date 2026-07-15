"""Mission activity reconciliation service."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl

MISSION_ACTIVITIES_BACKFILL_META_KEY = "mission_activities_backfill_cr_p3_1"


class MissionActivityReconciler:
    """Backfill legacy mission activities from authoritative mission links."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._repo = TeamMissionRepoImpl(conn)

    def reconcile(self) -> dict[str, Any]:
        marker = self._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (MISSION_ACTIVITIES_BACKFILL_META_KEY,),
        ).fetchone()
        if marker and str(marker["value"] or "") == "1":
            return {"ran": False, "inserted": 0}
        try:
            rows = self._rows()
        except sqlite3.OperationalError:
            rows = []
        inserted = 0
        for row in rows:
            mission_id = str(row["active_mission_id"] or "").strip()
            conversation_session_id = str(row["conversation_session_id"] or row["conversation_id"] or "").strip()
            if not mission_id or not conversation_session_id:
                continue
            existed = self._conn.execute(
                """
                SELECT 1
                FROM activities
                WHERE kind = 'mission' AND target_mission_id = ?
                LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
            self._repo.ensure_legacy_mission_activity(
                conversation_id=conversation_session_id,
                mission_id=mission_id,
                status="running",
                prompt_summary=str(row["title"] or ""),
                now=float(row["updated_at"] or row["created_at"] or time.time()),
            )
            if existed is None:
                inserted += 1
        self._conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'",
            (MISSION_ACTIVITIES_BACKFILL_META_KEY,),
        )
        return {"ran": True, "inserted": inserted}

    def _rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT DISTINCT
                tmc.conversation_id,
                tmc.conversation_session_id,
                cm.mission_id AS active_mission_id,
                tmc.title,
                tmc.created_at,
                cm.updated_at
            FROM conversation_missions cm
            JOIN team_mission_conversations tmc
              ON tmc.conversation_id = cm.conversation_id
            WHERE COALESCE(cm.mission_id, '') != ''
            UNION
            SELECT
                tmc.conversation_id,
                tmc.conversation_session_id,
                tmc.active_mission_id,
                tmc.title,
                tmc.created_at,
                tmc.updated_at
            FROM team_mission_conversations tmc
            WHERE COALESCE(tmc.active_mission_id, '') != ''
            """
        ).fetchall()


__all__ = ["MISSION_ACTIVITIES_BACKFILL_META_KEY", "MissionActivityReconciler"]
