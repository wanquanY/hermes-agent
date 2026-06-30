"""Tool-result handoff controls for deterministic agent turn boundaries.

Some tools do not produce a final answer for the user. They transfer ownership
to another runtime, scheduler, or control plane. The model must not receive
that tool result and continue acting as if the task is complete.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


TEAM_MISSION_HANDOFF_KIND = "team_mission_started"
CONTROL_FIELD = "hermes_control"


def extract_tool_handoff(tool_name: str, tool_result: Any) -> dict[str, Any] | None:
    """Return a turn-control contract embedded in a tool result, if present."""
    payload = _parse_json_object(tool_result)
    if not payload:
        return None
    control = payload.get(CONTROL_FIELD)
    if not isinstance(control, Mapping):
        return None
    kind = _text(control.get("kind"))
    if kind != TEAM_MISSION_HANDOFF_KIND:
        return None
    if _text(tool_name) != "team_mission_start_task":
        return None
    if payload.get("success") is not True:
        return None
    end_current_turn = control.get("end_current_turn") is True
    skip_remaining_tool_calls = end_current_turn or control.get("skip_remaining_tool_calls") is True
    if not skip_remaining_tool_calls:
        return None

    node = payload.get("node") if isinstance(payload.get("node"), Mapping) else {}
    return {
        "kind": kind,
        "tool_name": "team_mission_start_task",
        "end_current_turn": end_current_turn,
        "skip_remaining_tool_calls": skip_remaining_tool_calls,
        "require_followup_response": bool(control.get("require_followup_response")),
        "mission_id": _text(payload.get("mission_id")),
        "conversation_id": _text(payload.get("conversation_id")),
        "task_id": _text(payload.get("task_id")),
        "node_id": _text(node.get("node_id") or node.get("id")),
        "submission_status": _text(payload.get("submission_status") or "accepted"),
        "task_status": _text(payload.get("task_status") or payload.get("mission_status") or "planning"),
        "await_final_deliverable": bool(
            payload.get("await_final_deliverable")
            or control.get("await_final_deliverable")
        ),
        "assistant_response": _text(
            control.get("assistant_response")
            or payload.get("assistant_response")
            or payload.get("message")
        ),
        "assistant_followup_instruction": _text(
            control.get("assistant_followup_instruction")
            or payload.get("assistant_followup_instruction")
        ),
    }


def record_tool_handoff(agent: Any, tool_name: str, tool_result: Any) -> bool:
    """Store the first handoff contract on the agent for the conversation loop."""
    handoff = extract_tool_handoff(tool_name, tool_result)
    if not handoff:
        return False
    if getattr(agent, "_tool_handoff_exit", None) is None:
        agent._tool_handoff_exit = handoff
    return True


def pop_tool_handoff(agent: Any) -> dict[str, Any] | None:
    """Read and clear the pending tool handoff from an agent."""
    handoff = getattr(agent, "_tool_handoff_exit", None)
    agent._tool_handoff_exit = None
    return dict(handoff) if isinstance(handoff, Mapping) else None


def format_tool_handoff_response(handoff: Mapping[str, Any] | None) -> str:
    """Build the controlled assistant response for a handoff turn."""
    data = handoff if isinstance(handoff, Mapping) else {}
    response = _text(data.get("assistant_response"))
    if response:
        return response

    if _text(data.get("kind")) == TEAM_MISSION_HANDOFF_KIND:
        return (
            "团队任务已启动，正在后台处理。你可以在右侧画布查看进度，也可以继续发送新的任务。"
        )

    return "The task was handed off to another runtime and this turn is now waiting for its final result."


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {}
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "CONTROL_FIELD",
    "TEAM_MISSION_HANDOFF_KIND",
    "extract_tool_handoff",
    "format_tool_handoff_response",
    "pop_tool_handoff",
    "record_tool_handoff",
]
