"""Asynchronous multi-agent team mission dispatch tool."""

from __future__ import annotations

from typing import Any

from tools.registry import registry, tool_result
from hermes_agent.orchestration.worker_rpc_proxy import get_default_worker_rpc_proxy


DISPATCH_TEAM_ASYNC_SCHEMA = {
    "name": "dispatch_team_async",
    "description": (
        "Dispatch a long-running multi-agent mission asynchronously. "
        "Returns immediately with activity and mission ids; does not wait "
        "for completion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "team_id": {
                "type": "string",
                "description": "Target team id.",
            },
            "mission_objective": {
                "type": "string",
                "description": "What the team should achieve.",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional file paths to include as mission context.",
            },
            "summary": {
                "type": "string",
                "description": "Optional short UI display summary for the dispatch activity.",
            },
        },
        "required": ["team_id", "mission_objective"],
    },
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parent_conversation_id(parent_agent: Any) -> str:
    return _text(
        getattr(parent_agent, "session_id", "")
        or getattr(parent_agent, "gateway_session_key", "")
        or getattr(parent_agent, "_session_id", "")
    )


def _parent_activity_id(parent_agent: Any) -> str:
    run_context = getattr(parent_agent, "run_context", None) or getattr(
        parent_agent,
        "_run_context",
        None,
    )
    return _text(getattr(run_context, "activity_id", ""))


def dispatch_team_async_tool(args: dict, **kwargs) -> dict[str, Any]:
    proxy = get_default_worker_rpc_proxy()
    if proxy is None:
        raise RuntimeError("dispatch_team_async requires worker IPC")

    parent_agent = kwargs.get("parent_agent")
    parent_conversation_id = _text(args.get("parent_conversation_id")) or _parent_conversation_id(
        parent_agent,
    )
    parent_activity_id = _text(args.get("parent_activity_id")) or _parent_activity_id(parent_agent)
    if not parent_conversation_id:
        raise ValueError("dispatch_team_async requires parent_conversation_id")

    params = {
        "target_team_id": _text(args.get("team_id") or args.get("target_team_id")),
        "mission_objective": str(args.get("mission_objective") or ""),
        "files": args.get("files") if isinstance(args.get("files"), list) else [],
        "summary": _text(args.get("summary")),
        "parent_conversation_id": parent_conversation_id,
    }
    if parent_activity_id:
        params["parent_activity_id"] = parent_activity_id

    result = proxy.request("worker.dispatch_team_async", params)
    return result if isinstance(result, dict) else {"result": result}


def _handle_dispatch_team_async(args: dict, **kwargs) -> str:
    return tool_result(dispatch_team_async_tool(args, **kwargs))


registry.register(
    name="dispatch_team_async",
    toolset="subagent",
    schema=DISPATCH_TEAM_ASYNC_SCHEMA,
    handler=_handle_dispatch_team_async,
    description=DISPATCH_TEAM_ASYNC_SCHEMA["description"],
)
