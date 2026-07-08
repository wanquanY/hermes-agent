"""Fetch an async activity by id through the worker DB proxy."""

from __future__ import annotations

from typing import Any

from tools.registry import registry, tool_error, tool_result
from hermes_agent.orchestration.worker_db_proxy import get_default_worker_db_proxy


GET_ACTIVITY_SCHEMA = {
    "name": "get_activity",
    "description": "Fetch an async activity by id, including result fields.",
    "parameters": {
        "type": "object",
        "properties": {
            "activity_id": {
                "type": "string",
                "description": "Activity id returned by dispatch_agent_async or dispatch_team_async.",
            },
        },
        "required": ["activity_id"],
    },
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def get_activity_tool(args: dict, **kwargs) -> dict[str, Any]:
    activity_id = _text((args or {}).get("activity_id") or (args or {}).get("activityId"))
    if not activity_id:
        raise ValueError("activity_id required")
    proxy = get_default_worker_db_proxy()
    if proxy is None:
        raise RuntimeError("get_activity requires worker DB IPC")
    row = proxy.get_activity(activity_id)
    if not row:
        return {"activity_id": activity_id, "found": False}
    result = dict(row)
    result["found"] = True
    return result


def _handle_get_activity(args: dict, **kwargs) -> str:
    try:
        return tool_result(get_activity_tool(args, **kwargs))
    except Exception as exc:
        return tool_error(str(exc))


registry.register(
    name="get_activity",
    toolset="subagent",
    schema=GET_ACTIVITY_SCHEMA,
    handler=_handle_get_activity,
    description=GET_ACTIVITY_SCHEMA["description"],
)
