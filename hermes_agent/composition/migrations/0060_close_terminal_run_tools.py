"""Repair tool invocations left open under terminal Runs."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_agent.domain.tool_lifecycle import build_orphaned_tool_completion
from hermes_agent.repositories.run_repo import RunRepoImpl


version = 60
description = "close unfinished tool invocations owned by terminal runs"


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return (
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _record(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def apply(cursor: sqlite3.Cursor) -> None:
    if not all(
        _table_exists(cursor, table)
        for table in ("runs", "run_events", "tool_events", "seq_counter")
    ):
        return
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(
        """
        SELECT
            te.*,
            r.status AS run_status,
            r.turn_id AS run_turn_id,
            r.execution_session_id AS run_execution_session_id,
            r.runtime_scope_key AS run_runtime_scope_key,
            r.updated_at AS run_updated_at
          FROM tool_events te
          JOIN runs r ON r.run_id = te.run_id
         WHERE r.status IN ('completed', 'failed', 'cancelled', 'interrupted')
           AND te.status NOT IN ('completed', 'failed', 'cancelled', 'interrupted')
         ORDER BY te.session_id, te.seq_start, te.tool_call_id
        """
    ).fetchall()
    repository = RunRepoImpl(cursor.connection)
    for row in rows:
        tool = dict(row)
        metadata = _record(tool.get("metadata_json"))
        cleanup = build_orphaned_tool_completion(
            tool,
            session_id=str(tool.get("session_id") or ""),
            run_id=str(tool.get("run_id") or ""),
            turn_id=str(
                tool.get("turn_id")
                or tool.get("run_turn_id")
                or ""
            ),
            execution_session_id=str(
                tool.get("run_execution_session_id") or ""
            ),
            runtime_scope_key=str(tool.get("run_runtime_scope_key") or ""),
            participant_id=str(tool.get("participant_id") or ""),
            activity_id=str(
                metadata.get("activity_id")
                or metadata.get("activityId")
                or ""
            ),
            run_status=str(tool.get("run_status") or "failed"),
            timestamp=float(tool.get("run_updated_at") or 0),
            repair=True,
        )
        repository.append_runtime_event(
            str(tool.get("session_id") or ""),
            cleanup,
            participant_id=str(tool.get("participant_id") or ""),
            activity_id=str(cleanup.get("activity_id") or ""),
        )


__all__ = ["apply", "description", "version"]
