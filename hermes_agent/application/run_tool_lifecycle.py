"""Application orchestration for parent-Run and child-tool terminal ordering."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from hermes_agent.domain.run_state_machine import terminal_status_from_event
from hermes_agent.domain.tool_lifecycle import (
    TERMINAL_TOOL_STATUSES,
    build_orphaned_tool_completion,
)


def _payload(frame: dict[str, Any]) -> dict[str, Any]:
    value = frame.get("payload")
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def close_open_tools_before_terminal(
    *,
    repository: Any,
    projection: Any,
    session_id: str,
    event: dict[str, Any],
    participant_id: str = "",
    activity_id: str = "",
) -> list[dict[str, Any]]:
    """Append child terminal facts before appending a parent terminal fact.

    The application service owns this cross-aggregate ordering.  The Run
    repository remains isolated to the Run aggregate and canonical event
    journal; the tool read model remains the sole projection reader.
    """
    payload = _payload(event)
    event_type = str(event.get("type") or "").strip()
    run_status = terminal_status_from_event(event_type, payload)
    run_id = _first_text(
        event.get("run_id"),
        event.get("runId"),
        payload.get("run_id"),
        payload.get("runId"),
    )
    if not run_status or not run_id:
        return []
    try:
        rows = projection.list(session_id, run_id=run_id, limit=2000)
    except sqlite3.OperationalError:
        # Minimal bootstrap schemas may not have installed the projection yet.
        return []
    closed: list[dict[str, Any]] = []
    for tool in rows:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("status") or "").strip().lower() in TERMINAL_TOOL_STATUSES:
            continue
        cleanup = build_orphaned_tool_completion(
            tool,
            session_id=session_id,
            run_id=run_id,
            turn_id=_first_text(
                event.get("turn_id"),
                event.get("turnId"),
                payload.get("turn_id"),
                payload.get("turnId"),
                tool.get("turn_id"),
            ),
            execution_session_id=_first_text(
                event.get("execution_session_id"),
                payload.get("execution_session_id"),
            ),
            runtime_scope_key=_first_text(
                event.get("runtime_scope_key"),
                payload.get("runtime_scope_key"),
                session_id,
            ),
            participant_id=_first_text(
                participant_id,
                event.get("participant_id"),
                payload.get("participant_id"),
                tool.get("participant_id"),
            ),
            activity_id=_first_text(
                activity_id,
                event.get("activity_id"),
                payload.get("activity_id"),
            ),
            run_status=run_status,
            timestamp=float(event.get("timestamp") or time.time()),
        )
        closed.append(
            repository.append_runtime_event(
                session_id,
                cleanup,
                participant_id=str(cleanup.get("participant_id") or ""),
                activity_id=str(cleanup.get("activity_id") or ""),
            )
        )
    return closed


__all__ = ["close_open_tools_before_terminal"]
