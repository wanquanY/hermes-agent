"""Projection-aware retention for the canonical run-event ledger."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_state_machine import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES
from hermes_agent.repositories.run_repo import RunRepoImpl
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork
from hermes_team_mission.runtime.run_event_retention import (
    DEFAULT_RUN_EVENT_MAX_PER_SESSION,
    DEFAULT_RUN_EVENT_RETENTION_DAYS,
    RunEventRetentionPolicy,
)


class RunEventRetentionService:
    """Owns event deletion and archive summaries outside the hot repository."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        unit_of_work: SqliteUnitOfWork,
    ) -> None:
        self._conn = conn
        self._unit_of_work = unit_of_work
        self._policy = RunEventRetentionPolicy()

    def maintain_after_append(
        self,
        *,
        session_id: str,
        run_id: str = "",
        seq: int = 0,
        terminal_status: str | None = None,
    ) -> None:
        terminal = str(terminal_status or "") in TERMINAL_RUN_STATUSES
        if not terminal and (int(seq or 0) <= 0 or int(seq) % 500 != 0):
            return
        if terminal and run_id:
            self.prune_terminal_streams(session_id=session_id, run_id=run_id)
        self.prune(session_id=session_id)

    def prune_terminal_streams(
        self,
        *,
        session_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        event_types = self._policy.terminal_prunable_event_types()
        if not stable or not normalized_run_id or not event_types:
            return {"deleted_events": 0, "event_types": list(event_types)}

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            placeholders = ",".join("?" for _ in event_types)
            terminal_statuses = ",".join("?" for _ in TERMINAL_RUN_STATUSES)
            rows = conn.execute(
                f"""
                SELECT e.*
                FROM run_events e
                JOIN runs r ON r.run_id = e.run_id
                WHERE e.session_id = ?
                  AND e.run_id = ?
                  AND e.event_type IN ({placeholders})
                  AND r.status IN ({terminal_statuses})
                ORDER BY e.seq ASC, e.id ASC
                """,
                (stable, normalized_run_id, *event_types, *sorted(TERMINAL_RUN_STATUSES)),
            ).fetchall()
            deletable = [
                row for row in rows
                if self._policy.can_delete_terminal_stream_row(conn, row)
            ]
            if not deletable:
                return {"deleted_events": 0, "event_types": list(event_types)}
            self._archive(conn, deletable, reason="terminal_run_stream_events")
            EventLedger(conn).delete_rows_by_id([int(row["id"]) for row in deletable])
            RunRepoImpl(conn).reset_last_seq_from_events(normalized_run_id)
            return {"deleted_events": len(deletable), "event_types": list(event_types)}

        return self._unit_of_work.execute(operation)

    def prune(
        self,
        *,
        session_id: str = "",
        retention_days: int = DEFAULT_RUN_EVENT_RETENTION_DAYS,
        max_events_per_session: int = DEFAULT_RUN_EVENT_MAX_PER_SESSION,
        now: float | None = None,
    ) -> dict[str, Any]:
        stable_filter = str(session_id or "").strip()
        cutoff = float(now or time.time()) - max(1, int(retention_days or 1)) * 86400
        max_per_session = max(100, int(max_events_per_session or DEFAULT_RUN_EVENT_MAX_PER_SESSION))

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            active_statuses = tuple(sorted(ACTIVE_RUN_STATUSES))
            active_placeholders = ",".join("?" for _ in active_statuses)
            age_params: list[Any] = [cutoff, *active_statuses]
            session_clause = ""
            if stable_filter:
                session_clause = "AND e.session_id = ?"
                age_params.append(stable_filter)
            aged = conn.execute(
                f"""
                SELECT e.*
                FROM run_events e
                LEFT JOIN runs r ON r.run_id = e.run_id
                WHERE e.timestamp < ?
                  AND COALESCE(r.status, '') NOT IN ({active_placeholders})
                  {session_clause}
                ORDER BY e.session_id, e.seq
                """,
                tuple(age_params),
            ).fetchall()
            deleted = self._delete(conn, aged, reason="retention_days")
            session_rows = conn.execute(
                "SELECT DISTINCT session_id FROM run_events"
                + (" WHERE session_id = ?" if stable_filter else ""),
                (stable_filter,) if stable_filter else (),
            ).fetchall()
            for session_row in session_rows:
                sid = str(session_row["session_id"] or "")
                rows = conn.execute(
                    f"""
                    SELECT e.*
                    FROM run_events e
                    LEFT JOIN runs r ON r.run_id = e.run_id
                    WHERE e.session_id = ?
                      AND COALESCE(r.status, '') NOT IN ({active_placeholders})
                    ORDER BY e.seq DESC
                    """,
                    (sid, *active_statuses),
                ).fetchall()
                deleted += self._delete(
                    conn,
                    list(reversed(rows[max_per_session:])),
                    reason="max_events_per_session",
                )
            return {
                "deleted_events": deleted,
                "retention_days": int(retention_days),
                "max_events_per_session": max_per_session,
            }

        return self._unit_of_work.execute(operation)

    def _delete(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        reason: str,
    ) -> int:
        if not rows:
            return 0
        self._archive(conn, rows, reason=reason)
        EventLedger(conn).delete_rows_by_id([int(row["id"]) for row in rows])
        return len(rows)

    @staticmethod
    def _archive(
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        reason: str,
    ) -> None:
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (str(row["session_id"] or ""), str(row["run_id"] or ""))
            grouped.setdefault(key, []).append(row)
        archived_at = time.time()
        for (session_id, run_id), group in grouped.items():
            seqs = [int(row["seq"] or 0) for row in group]
            timestamps = [float(row["timestamp"] or 0) for row in group]
            conn.execute(
                """
                INSERT INTO run_event_archives (
                    session_id, run_id, archived_at, first_seq, last_seq,
                    first_timestamp, last_timestamp, event_count, reason,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    archived_at,
                    min(seqs),
                    max(seqs),
                    min(timestamps),
                    max(timestamps),
                    len(group),
                    reason,
                    json.dumps({"policy": "run_event_retention"}, separators=(",", ":")),
                ),
            )


__all__ = ["RunEventRetentionService"]
