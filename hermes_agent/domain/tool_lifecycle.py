"""Cross-layer invariants for tool invocations owned by a Run."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


TERMINAL_TOOL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)


def tool_terminal_status_for_run(run_status: str) -> str:
    """Map a parent Run terminal state to an unfinished child invocation."""
    normalized = str(run_status or "").strip().lower()
    if normalized == "interrupted":
        return "interrupted"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    return "failed"


def build_orphaned_tool_completion(
    tool: Mapping[str, Any],
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    execution_session_id: str,
    runtime_scope_key: str,
    participant_id: str,
    activity_id: str,
    run_status: str,
    timestamp: float,
    repair: bool = False,
) -> dict[str, Any]:
    """Build the canonical terminal fact for an invocation left open by a Run."""
    tool_call_id = str(
        tool.get("tool_call_id")
        or tool.get("tool_id")
        or tool.get("id")
        or ""
    ).strip()
    tool_name = str(
        tool.get("tool_name")
        or tool.get("name")
        or ""
    ).strip()
    status = tool_terminal_status_for_run(run_status)
    if status == "interrupted":
        message = "Tool generation was interrupted before execution completed."
    elif status == "cancelled":
        message = "Tool generation was cancelled before execution completed."
    else:
        message = "Parent run ended before the tool invocation completed."
    payload: dict[str, Any] = {
        "tool_id": tool_call_id,
        "name": tool_name,
        "status": status,
        "error": message,
        "error_code": "parent_run_terminal",
        "result_text": message,
        "summary": "Tool did not complete before the parent run ended",
    }
    arguments = tool.get("arguments")
    if arguments is None:
        raw_arguments = tool.get("arguments_json")
        if isinstance(raw_arguments, str) and raw_arguments:
            try:
                arguments = json.loads(raw_arguments)
            except (TypeError, ValueError):
                arguments = None
    if arguments is not None:
        payload["arguments"] = arguments
    if repair:
        payload["lifecycle_repair"] = True
    frame: dict[str, Any] = {
        "type": "tool.complete",
        "conversation_session_id": session_id,
        "session_id": session_id,
        "execution_session_id": execution_session_id,
        "runtime_scope_key": runtime_scope_key,
        "run_id": run_id,
        "turn_id": turn_id,
        "participant_id": participant_id,
        "timestamp": timestamp,
        "payload": payload,
    }
    if activity_id:
        frame["activity_id"] = activity_id
    return frame


__all__ = [
    "TERMINAL_TOOL_STATUSES",
    "build_orphaned_tool_completion",
    "tool_terminal_status_for_run",
]
