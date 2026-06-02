"""Direct terminal responses for deterministic tool results.

Most tools feed results back into the model because the next step requires
language reasoning. A small class of Doxie action tools already returns a
complete, structured success event; asking the model for a follow-up sentence
adds latency and can leave the turn running if the follow-up stream stalls.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any


_DIRECT_DOXIE_AUTOMATION_EVENTS = {
    "doxie_automation_task_create": ("automation_job_created", "created"),
    "doxie_automation_task_update": ("automation_job_updated", "updated"),
    "doxie_automation_task_remove": ("automation_job_removed", "removed"),
}


def build_direct_tool_response(
    tool_calls: Sequence[Any],
    messages: Sequence[dict[str, Any]],
) -> str | None:
    """Return a final assistant response when a tool result is terminal.

    The guard is intentionally narrow: only a single supported Doxie action
    tool with a matching structured success event can bypass the normal
    model-follow-up turn.
    """
    if len(tool_calls) != 1:
        return None

    tool_call = tool_calls[0]
    tool_name = _tool_call_name(tool_call)
    event_config = _DIRECT_DOXIE_AUTOMATION_EVENTS.get(tool_name)
    if not event_config:
        return None
    expected_event, action = event_config

    tool_message = _latest_matching_tool_message(
        messages,
        tool_name=tool_name,
        tool_call_id=_tool_call_id(tool_call),
    )
    if not tool_message:
        return None

    content = tool_message.get("content")
    if not isinstance(content, str):
        return None

    try:
        payload = json.loads(content)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("doxie_event") != expected_event:
        return None

    job = payload.get("job")
    if isinstance(job, dict):
        return _format_automation_job_action(job, action=action)

    return None


def _tool_call_name(tool_call: Any) -> str:
    function = getattr(tool_call, "function", None)
    if function is not None:
        return str(getattr(function, "name", "") or "")
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
    return ""


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def _latest_matching_tool_message(
    messages: Sequence[dict[str, Any]],
    *,
    tool_name: str,
    tool_call_id: str,
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        if tool_call_id and str(message.get("tool_call_id") or "") != tool_call_id:
            continue
        message_name = str(message.get("name") or message.get("tool_name") or "")
        if tool_name and message_name != tool_name:
            continue
        return message
    return None


def _format_automation_job_action(job: dict[str, Any], *, action: str) -> str:
    name = _text(job.get("name")) or "自动化任务"
    verb = {
        "created": "已创建",
        "updated": "已更新",
        "removed": "已删除",
    }.get(action, "已处理")
    lines = [f"{verb}定时任务「{name}」。"]

    if action != "removed":
        schedule = _schedule_display(job)
        if schedule:
            lines.append(f"执行时间：{schedule}")

        enabled = job.get("enabled")
        if isinstance(enabled, bool):
            lines.append(f"状态：{'已启用' if enabled else '已暂停'}")

        next_run = _next_run_display(job)
        if next_run:
            lines.append(f"下次运行：{next_run}")

    job_id = _text(job.get("id") or job.get("jobId"))
    if job_id:
        lines.append(f"任务 ID：{job_id}")

    return "\n".join(lines)


def _schedule_display(job: dict[str, Any]) -> str:
    display = _text(job.get("scheduleDisplay"))
    if display:
        return display
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        return _text(
            schedule.get("text")
            or schedule.get("display")
            or schedule.get("expr")
        )
    return _text(schedule)


def _next_run_display(job: dict[str, Any]) -> str:
    state = job.get("state") if isinstance(job.get("state"), dict) else {}
    raw = state.get("nextRunAtMs")
    if not isinstance(raw, (int, float)) or raw <= 0:
        return ""
    try:
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M"
        )
    except Exception:
        return ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


__all__ = ["build_direct_tool_response"]
