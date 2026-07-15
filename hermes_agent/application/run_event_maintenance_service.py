"""Maintenance operations for canonical run-event storage and projections."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_event_activity_backfill import (
    ACTIVITY_ID_BACKFILL_DONE_KEY,
    ACTIVITY_ID_BACKFILL_PROGRESS_KEY,
    activity_id_for_legacy_event,
)
from hermes_agent.application.run_event_compaction import RunEventCompactor
from hermes_agent.domain.run_event_codec import (
    decode_run_event_row,
    update_run_event_frame_columns,
)
from hermes_agent.domain.run_event_index import (
    project_run_event_search_index,
    runtime_source_seq_from_event,
)
from hermes_agent.domain.run_event_reference import (
    reference_projected_run_event_payloads,
)
from hermes_agent.domain.session_runtime_state import session_info_payload_hash
from hermes_agent.application.state_metadata_service import StateMetadataService
from hermes_agent.read_models.tool_events import backfill_tool_events_from_run_events
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_team_mission.runtime.run_event_retention import RunEventRetentionPolicy


class RunEventMaintenanceService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        unit_of_work: SqliteUnitOfWork,
        metadata: StateMetadataService,
    ) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._unit_of_work = unit_of_work
        self._metadata = metadata
        self._retention = RunEventRetentionPolicy()
        self._compactor = RunEventCompactor(self._retention)

    def compact(
        self,
        *,
        session_id: str = "",
        vacuum: bool = False,
    ) -> dict[str, Any]:
        result = self._unit_of_work.execute(
            lambda conn: self._compactor.compact(conn, session_id=session_id)
        )
        if vacuum and int(result.get("deleted_events") or 0) > 0:
            with self._lock:
                self._conn.execute("VACUUM")
            result = {**result, "vacuumed": True}
        return result

    def maybe_auto_compact(
        self,
        *,
        min_interval_hours: int = 24,
        vacuum: bool = True,
    ) -> dict[str, Any]:
        now = time.time()
        try:
            last_run = float(
                self._metadata.get("last_auto_run_event_compaction_v1") or 0
            )
        except (TypeError, ValueError):
            last_run = 0.0
        interval_seconds = max(0, int(min_interval_hours or 0)) * 3600
        if last_run and now - last_run < interval_seconds:
            return {"skipped": True, "reason": "interval"}
        result = self.compact(vacuum=vacuum)
        self._metadata.set("last_auto_run_event_compaction_v1", str(now))
        return {"skipped": False, **result}

    def prune_duplicate_session_info(
        self,
        *,
        session_id: str = "",
    ) -> dict[str, int]:
        stable_filter = str(session_id or "").strip()

        def operation(conn: sqlite3.Connection) -> dict[str, int]:
            params: tuple[str, ...] = (stable_filter,) if stable_filter else ()
            session_clause = "AND session_id = ?" if stable_filter else ""
            rows = conn.execute(
                f"""
                SELECT *
                  FROM run_events
                 WHERE event_type = 'session.info'
                   {session_clause}
                 ORDER BY session_id ASC, seq ASC, id ASC
                """,
                params,
            ).fetchall()
            previous_by_identity: dict[
                tuple[str, str, str, str, str], tuple[str, sqlite3.Row]
            ] = {}
            duplicates: list[sqlite3.Row] = []
            for row in rows:
                event = decode_run_event_row(row)
                payload = (
                    event.get("payload")
                    if isinstance(event.get("payload"), dict)
                    else {}
                )
                identity = (
                    str(event.get("conversation_session_id") or row["session_id"] or ""),
                    str(event.get("runtime_scope_key") or row["runtime_scope_key"] or ""),
                    str(
                        event.get("execution_session_id")
                        or event.get("session_id")
                        or row["execution_session_id"]
                        or ""
                    ),
                    str(event.get("run_id") or row["run_id"] or ""),
                    str(event.get("turn_id") or row["turn_id"] or ""),
                )
                payload_hash = session_info_payload_hash(payload)
                previous = previous_by_identity.get(identity)
                if previous is not None and previous[0] == payload_hash:
                    duplicates.append(previous[1])
                previous_by_identity[identity] = (payload_hash, row)
            if not duplicates:
                return {"deleted_events": 0}
            ledger = EventLedger(conn)
            ledger.archive_rows(duplicates, reason="duplicate_session_info")
            ledger.delete_rows_by_id(int(row["id"]) for row in duplicates)
            return {"deleted_events": len(duplicates)}

        return self._unit_of_work.execute(operation)

    def backfill_frames(
        self,
        *,
        session_id: str = "",
        limit: int = 5000,
    ) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        bounded_limit = max(1, min(int(limit or 5000), 20000))

        def operation(conn):
            ledger = EventLedger(conn)
            rows = ledger.list_frame_backfill_rows(
                session_id=stable,
                limit=bounded_limit,
            )
            for row in rows:
                event = decode_run_event_row(row)
                runtime_source_seq = runtime_source_seq_from_event(event)
                event_type = str(row["event_type"] or event.get("type") or "")
                update_run_event_frame_columns(
                    conn,
                    row_id=int(row["id"]),
                    event=event,
                    retention_class=self._retention.classify_event_type(event_type),
                    projection_state="raw",
                )
                ledger.update_runtime_source_seq(
                    row_id=int(row["id"]),
                    runtime_source_seq=runtime_source_seq,
                )
                project_run_event_search_index(
                    conn,
                    row_id=int(row["id"]),
                    session_id=str(
                        row["session_id"]
                        or event.get("conversation_session_id")
                        or ""
                    ),
                    seq=int(row["seq"] or event.get("seq") or 0),
                    event_type=event_type,
                    runtime_scope_key=str(
                        row["runtime_scope_key"]
                        or event.get("runtime_scope_key")
                        or ""
                    ),
                    runtime_source_seq=runtime_source_seq,
                    event=event,
                    updated_at=float(
                        row["timestamp"] or event.get("timestamp") or 0
                    ),
                )
            return {
                "updated_events": len(rows),
                "remaining_events": ledger.count_frame_backfill_rows(
                    session_id=stable,
                ),
                "limit": bounded_limit,
            }

        return self._unit_of_work.execute(operation)

    def backfill_activity_ids(
        self,
        *,
        dry_run: bool = False,
        batch_size: int = 5000,
    ) -> dict[str, Any]:
        if self._metadata.get(ACTIVITY_ID_BACKFILL_DONE_KEY):
            return {
                "skipped": True,
                "reason": "done",
                "scanned": 0,
                "updated": 0,
            }
        try:
            after_id = int(
                self._metadata.get(ACTIVITY_ID_BACKFILL_PROGRESS_KEY) or 0
            )
        except (TypeError, ValueError):
            after_id = 0
        bounded_batch_size = max(1, min(int(batch_size or 5000), 20000))
        started = time.monotonic()

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            ledger = EventLedger(conn)
            rows = ledger.list_activity_id_backfill_rows(
                after_id=after_id,
                limit=bounded_batch_size,
            )
            next_max_id = after_id
            updates: list[tuple[int, str]] = []
            for row in rows:
                row_id = int(row["id"] or 0)
                next_max_id = max(next_max_id, row_id)
                activity_id = activity_id_for_legacy_event(row)
                if activity_id:
                    updates.append((row_id, activity_id))
            if not dry_run:
                for row_id, activity_id in updates:
                    ledger.mark_activity_id(
                        row_id=row_id,
                        activity_id=activity_id,
                    )
            return {
                "scanned": len(rows),
                "updated": 0 if dry_run else len(updates),
                "next_max_id": next_max_id,
                "done": not ledger.has_activity_id_backfill_rows(
                    after_id=next_max_id,
                ),
            }

        result = self._unit_of_work.execute(operation)
        if not dry_run:
            next_max_id = int(result.get("next_max_id") or 0)
            if next_max_id > after_id:
                self._metadata.set(
                    ACTIVITY_ID_BACKFILL_PROGRESS_KEY,
                    str(next_max_id),
                )
            if result.get("done"):
                self._metadata.set(
                    ACTIVITY_ID_BACKFILL_DONE_KEY,
                    str(int(time.time())),
                )
        return {
            "skipped": False,
            **result,
            "duration_s": time.monotonic() - started,
        }

    def reference_payloads(
        self,
        *,
        session_id: str = "",
        limit: int = 1000,
    ) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        bounded_limit = max(1, min(int(limit or 1000), 20000))
        return self._unit_of_work.execute(
            lambda conn: reference_projected_run_event_payloads(
                conn,
                session_id=stable,
                limit=bounded_limit,
            )
        )

    def rebuild_tool_event_projection(self) -> None:
        self._unit_of_work.execute(backfill_tool_events_from_run_events)


__all__ = ["RunEventMaintenanceService"]
