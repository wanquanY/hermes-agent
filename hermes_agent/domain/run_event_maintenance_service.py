"""Maintenance operations for canonical run-event storage and projections."""

from __future__ import annotations

from typing import Any

from hermes_agent.domain.event_ledger import EventLedger
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
from hermes_agent.read_models.tool_events import backfill_tool_events_from_run_events
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork
from hermes_team_mission.runtime.run_event_retention import RunEventRetentionPolicy


class RunEventMaintenanceService:
    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        self._unit_of_work = unit_of_work
        self._retention = RunEventRetentionPolicy()

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
