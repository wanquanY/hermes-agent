"""Session index read-model reconciliation.

This L2 domain service may read across session/run/team tables to decide what
the sidebar projection should look like. Physical writes to ``session_index``
still go through ``SessionRepoImpl`` so the session aggregate remains the only
repository owner for that table family.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from hermes_agent.repositories.session_repo import SessionIndexPatch, SessionRepoImpl


class SessionIndexReconciler:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sessions = SessionRepoImpl(conn)

    def reconcile(self, *, exclude_sources: list[str] | None = None) -> dict[str, Any]:
        excluded = tuple(exclude_sources if exclude_sources is not None else ("tool", "cron"))
        placeholders = ", ".join("?" for _ in excluded) if excluded else ""
        team_internal_clause = (
            "NOT (id LIKE 'team:%' AND id LIKE '%:node:%') "
            "AND NOT (COALESCE(parent_session_id,'') LIKE 'team:%:node:%') "
        )
        subagent_clause = (
            "NOT ("
            "  COALESCE(parent_session_id,'') != ''"
            "  AND NOT EXISTS (SELECT 1 FROM session_lineage l WHERE l.session_id = sessions.id)"
            "  AND EXISTS ("
            "    SELECT 1 FROM sessions p"
            "    WHERE p.id = sessions.parent_session_id"
            "    AND COALESCE(p.end_reason,'') NOT IN ('compression', 'compression_split')"
            "  )"
            ")"
        )
        where = [team_internal_clause, subagent_clause]
        params: list[Any] = []
        if excluded:
            where.append(f"COALESCE(source,'') NOT IN ({placeholders})")
            params.extend(excluded)
        select_sql = (
            "SELECT id, source, title, display_title, preview, started_at, "
            "last_active, message_count, transient FROM sessions WHERE "
            + " AND ".join(where)
        )

        self._delete_internal_runtime_index_rows()
        rows = self._conn.execute(select_sql, tuple(params)).fetchall()
        upserted = 0
        for row in rows:
            started = float(row["started_at"] or 0)
            updated = float(row["last_active"] or row["started_at"] or 0)
            title = str(row["display_title"] or row["title"] or "")
            self._sessions.upsert_session_index(
                {
                    "session_id": str(row["id"]),
                    "title": title,
                    "preview": str(row["preview"] or ""),
                    "source": str(row["source"] or "unknown"),
                    "transient": 1 if row["transient"] else 0,
                    "conversation_kind": (
                        "team" if str(row["source"] or "") == "team_mission" else "direct"
                    ),
                    "message_count": int(row["message_count"] or 0),
                    "started_at": started,
                    "updated_at": updated,
                    "last_activity": updated,
                }
            )
            upserted += 1
        self._repair_inactive_team_mission_conversation_links()
        self._repair_team_terminal_session_index()
        self.repair_terminal_active_runs()
        return {"reconciled": upserted}

    def repair_terminal_active_runs(self) -> int:
        active_run_exists = _session_index_active_run_exists_sql("si")
        active_mission_exists = _session_index_active_mission_exists_sql("si")
        active_run_id_is_terminal = """
            EXISTS (
                SELECT 1
                  FROM runs indexed_active_run
                 WHERE indexed_active_run.run_id = si.active_run_id
                   AND LOWER(COALESCE(indexed_active_run.status,'')) IN
                       ('completed','failed','cancelled','canceled','interrupted')
            )
        """
        rows = self._conn.execute(
            f"""
            SELECT si.session_id
              FROM session_index si
             WHERE si.active_run_id != ''
               AND ({active_run_id_is_terminal})
               AND NOT ({active_run_exists})
               AND (
                   si.conversation_kind != 'team'
                   OR NOT ({active_mission_exists})
               )
            """
        ).fetchall()
        session_ids = [str(row["session_id"] or "") for row in rows]
        for session_id in session_ids:
            self._sessions.update_index(
                session_id,
                SessionIndexPatch(
                    status="idle",
                    running=0,
                    waiting_approval=0,
                    active_run_id="",
                    active_execution_session_id="",
                    pending_approval_count=0,
                ),
            )
        return len(session_ids)

    def _delete_internal_runtime_index_rows(self) -> None:
        rows = self._conn.execute(
            """
            SELECT session_id
              FROM session_index
             WHERE session_id LIKE 'team:%' AND session_id LIKE '%:node:%'
                OR session_id IN (
                    SELECT id FROM sessions
                     WHERE COALESCE(parent_session_id,'') LIKE 'team:%:node:%'
                )
                OR session_id IN (
                    SELECT s.id FROM sessions s
                     WHERE COALESCE(s.parent_session_id,'') != ''
                       AND NOT EXISTS (
                           SELECT 1 FROM session_lineage l WHERE l.session_id = s.id
                       )
                       AND EXISTS (
                           SELECT 1 FROM sessions p
                            WHERE p.id = s.parent_session_id
                              AND COALESCE(p.end_reason,'') NOT IN (
                                  'compression', 'compression_split'
                              )
                       )
                )
            """
        ).fetchall()
        for row in rows:
            self._sessions.delete_index(str(row["session_id"] or ""))

    def _repair_inactive_team_mission_conversation_links(self) -> None:
        inactive_conversation_count = int(
            self._conn.execute(
                """
                SELECT COUNT(*)
                  FROM conversation_missions cm
                  JOIN team_missions tm ON tm.mission_id = cm.mission_id
                 WHERE cm.status = 'active'
                   AND LOWER(COALESCE(tm.status,'')) IN
                       ('completed','failed','cancelled','canceled','interrupted','draft','idle')
                """
            ).fetchone()[0]
            or 0
        )
        if not inactive_conversation_count:
            return
        self._conn.execute(
            """
            UPDATE conversation_missions
               SET status = CASE LOWER(COALESCE((
                        SELECT tm.status
                          FROM team_missions tm
                         WHERE tm.mission_id = conversation_missions.mission_id
                    ), ''))
                    WHEN 'completed' THEN 'completed'
                    WHEN 'failed' THEN 'failed'
                    ELSE 'cancelled'
                   END,
                   updated_at = ?
             WHERE status = 'active'
               AND mission_id IN (
                   SELECT mission_id
                     FROM team_missions
                    WHERE LOWER(COALESCE(status,'')) IN
                          ('completed','failed','cancelled','canceled','interrupted','draft','idle')
               )
            """,
            (time.time(),),
        )

    def _repair_team_terminal_session_index(self) -> None:
        active_run_exists = _session_index_active_run_exists_sql("si")
        active_mission_exists = _session_index_active_mission_exists_sql("si")
        active_run_id_is_terminal = """
            EXISTS (
                SELECT 1
                  FROM runs indexed_active_run
                 WHERE indexed_active_run.run_id = si.active_run_id
                   AND LOWER(COALESCE(indexed_active_run.status,'')) IN
                       ('completed','failed','cancelled','canceled','interrupted')
            )
        """
        terminal_rows = self._conn.execute(
            f"""
            SELECT si.session_id
              FROM session_index si
             WHERE si.conversation_kind = 'team'
               AND (si.running = 1 OR si.waiting_approval = 1 OR si.status != 'idle'
                    OR si.active_run_id != '' OR si.active_execution_session_id != '')
               AND si.mission_id IN (
                   SELECT mission_id FROM team_missions
                    WHERE LOWER(COALESCE(status,'')) IN
                          ('completed','failed','cancelled','canceled','interrupted')
               )
               AND (si.active_run_id = '' OR ({active_run_id_is_terminal}))
               AND NOT ({active_run_exists})
               AND NOT ({active_mission_exists})
            """
        ).fetchall()
        draft_rows = self._conn.execute(
            f"""
            SELECT si.session_id
              FROM session_index si
             WHERE si.conversation_kind = 'team'
               AND si.waiting_approval = 1
               AND si.mission_id IN (
                   SELECT mission_id FROM team_missions
                    WHERE LOWER(COALESCE(status,'')) IN ('draft', 'idle')
               )
               AND NOT EXISTS (
                   SELECT 1 FROM team_mission_nodes n
                    WHERE n.mission_id = si.mission_id
                      AND LOWER(COALESCE(n.status,'')) IN
                          ('waiting_approval','running','starting')
               )
               AND NOT ({active_run_exists})
               AND NOT ({active_mission_exists})
            """
        ).fetchall()
        for row in [*terminal_rows, *draft_rows]:
            self._sessions.update_index(
                str(row["session_id"] or ""),
                SessionIndexPatch(
                    status="idle",
                    running=0,
                    waiting_approval=0,
                    active_run_id="",
                    active_execution_session_id="",
                    pending_approval_count=0,
                ),
            )


def _session_index_active_run_exists_sql(session_alias: str = "session_index") -> str:
    si = session_alias
    terminal = "'completed','failed','cancelled','canceled','interrupted'"
    return f"""
        EXISTS (
            SELECT 1
              FROM runs active_runs
             WHERE LOWER(COALESCE(active_runs.status,'')) NOT IN ({terminal})
               AND (
                   active_runs.session_id = {si}.session_id
                   OR (
                       COALESCE({si}.conversation_id, '') != ''
                       AND EXISTS (
                           SELECT 1
                             FROM team_mission_conversations active_tmc
                            WHERE active_tmc.conversation_id = {si}.conversation_id
                              AND (
                                  active_tmc.conversation_session_id = active_runs.session_id
                                  OR active_tmc.conversation_id = active_runs.session_id
                              )
                       )
                   )
                   OR (
                       COALESCE({si}.conversation_id, '') != ''
                       AND EXISTS (
                           SELECT 1
                             FROM team_mission_run_bindings active_binding
                             JOIN team_missions active_mission
                               ON active_mission.mission_id = active_binding.mission_id
                            WHERE active_binding.run_id = active_runs.run_id
                              AND active_mission.conversation_id = {si}.conversation_id
                       )
                   )
                   OR (
                       COALESCE({si}.conversation_id, '') = ''
                       AND COALESCE({si}.mission_id, '') != ''
                       AND EXISTS (
                           SELECT 1
                             FROM team_mission_run_bindings active_binding
                             JOIN team_missions active_mission
                               ON active_mission.mission_id = active_binding.mission_id
                            WHERE active_binding.run_id = active_runs.run_id
                              AND active_mission.conversation_id IN (
                                  SELECT indexed_mission.conversation_id
                                    FROM team_missions indexed_mission
                                   WHERE indexed_mission.mission_id = {si}.mission_id
                              )
                       )
                   )
               )
        )
    """


def _session_index_active_mission_exists_sql(session_alias: str = "session_index") -> str:
    si = session_alias
    inactive_statuses = "'completed','failed','cancelled','canceled','interrupted','draft','idle'"
    return f"""
        EXISTS (
            SELECT 1
              FROM conversation_missions active_cm
              JOIN team_missions active_tm
                ON active_tm.mission_id = active_cm.mission_id
             WHERE active_cm.status = 'active'
               AND LOWER(COALESCE(active_tm.status,'')) NOT IN
                   ({inactive_statuses})
               AND (
                   (
                       COALESCE({si}.conversation_id, '') != ''
                       AND active_cm.conversation_id = {si}.conversation_id
                   )
                   OR (
                       COALESCE({si}.conversation_id, '') = ''
                       AND COALESCE({si}.mission_id, '') != ''
                       AND active_cm.conversation_id IN (
                           SELECT indexed_mission.conversation_id
                             FROM team_missions indexed_mission
                            WHERE indexed_mission.mission_id = {si}.mission_id
                       )
                   )
               )
        )
    """
