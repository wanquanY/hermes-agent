"""Synchronous in-process subagent invocation tool."""

from __future__ import annotations

from agent.subagent_invoke import invoke_subagent
from tools.registry import registry, tool_result


INVOKE_SUBAGENT_SCHEMA = {
    "name": "invoke_subagent",
    "description": (
        "Invoke another Hermes agent profile as a synchronous in-process "
        "subagent for short tasks. The caller blocks until the subagent "
        "returns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_profile_id": {
                "type": "string",
                "description": "Target agent profile id. Use 'default' or 'agent-default' for the default profile.",
            },
            "prompt": {
                "type": "string",
                "description": "Complete task prompt to send to the subagent.",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional file paths to include as read-only context.",
            },
            "tools_subset": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional exact tool-name allowlist for the subagent.",
            },
        },
        "required": ["target_profile_id", "prompt"],
    },
}


def _handle_invoke_subagent(args: dict, **kwargs) -> str:
    parent_agent = kwargs.get("parent_agent")
    if parent_agent is None:
        raise ValueError("invoke_subagent requires parent_agent")

    result = invoke_subagent(
        parent_agent,
        str(args.get("target_profile_id") or ""),
        str(args.get("prompt") or ""),
        files=args.get("files"),
        tools_subset=args.get("tools_subset"),
    )
    return tool_result(result.to_dict())


registry.register(
    name="invoke_subagent",
    toolset="subagent",
    schema=INVOKE_SUBAGENT_SCHEMA,
    handler=_handle_invoke_subagent,
    description=INVOKE_SUBAGENT_SCHEMA["description"],
)
