"""Unified middleware boundary for agent-owned tool execution.

The agent loop owns several tools that bypass the global registry. Keeping the
middleware boundary here ensures built-in, plugin, context-engine, and registry
tools all follow the same request -> policy -> execution lifecycle.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _context(
    agent: Any,
    *,
    task_id: str,
    tool_call_id: str,
) -> dict[str, str]:
    return {
        "task_id": task_id or "",
        "session_id": getattr(agent, "session_id", "") or "",
        "tool_call_id": tool_call_id or "",
        "turn_id": getattr(agent, "_current_turn_id", "") or "",
        "api_request_id": getattr(agent, "_current_api_request_id", "") or "",
    }


def apply_agent_tool_request(
    agent: Any,
    *,
    function_name: str,
    function_args: dict[str, Any],
    task_id: str,
    tool_call_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply request middleware before policy, approvals, and checkpoints."""
    try:
        from hermes_cli.middleware import apply_tool_request_middleware

        result = apply_tool_request_middleware(
            function_name,
            function_args,
            **_context(agent, task_id=task_id, tool_call_id=tool_call_id),
        )
        effective = result.payload if isinstance(result.payload, dict) else function_args
        return effective, list(result.trace)
    except Exception as exc:
        logger.debug("tool_request middleware failed open: %s", exc)
        return function_args, []


def run_agent_tool_execution(
    agent: Any,
    *,
    function_name: str,
    function_args: dict[str, Any],
    task_id: str,
    tool_call_id: str,
    execute: Callable[[dict[str, Any]], Any],
) -> tuple[Any, dict[str, Any]]:
    """Run one tool through execution middleware and report effective args."""
    observed_args = function_args

    def terminal(next_args: dict[str, Any]) -> Any:
        nonlocal observed_args
        observed_args = next_args if isinstance(next_args, dict) else function_args
        return execute(observed_args)

    from hermes_cli.middleware import run_tool_execution_middleware

    result = run_tool_execution_middleware(
        function_name,
        function_args,
        terminal,
        original_args=function_args,
        **_context(agent, task_id=task_id, tool_call_id=tool_call_id),
    )
    return result, observed_args


def invoke_agent_tool(
    agent: Any,
    function_name: str,
    function_args: dict[str, Any],
    task_id: str,
    tool_call_id: Optional[str] = None,
    messages: Optional[list[Any]] = None,
    pre_tool_block_checked: bool = False,
    middleware_applied: bool = False,
    middleware_trace: Optional[list[dict[str, Any]]] = None,
) -> Any:
    """Invoke any agent tool under the shared middleware contract."""
    call_id = tool_call_id or ""
    trace = list(middleware_trace or [])
    if not middleware_applied:
        function_args, trace = apply_agent_tool_request(
            agent,
            function_name=function_name,
            function_args=function_args,
            task_id=task_id,
            tool_call_id=call_id,
        )

    if not pre_tool_block_checked:
        from hermes_cli.plugins import resolve_pre_tool_block

        block_message = resolve_pre_tool_block(
            function_name,
            function_args,
            middleware_trace=trace,
            **_context(agent, task_id=task_id, tool_call_id=call_id),
        )
        if block_message is not None:
            return json.dumps({"error": block_message}, ensure_ascii=False)

    def dispatch(next_args: dict[str, Any]) -> Any:
        return _dispatch_agent_tool(
            agent,
            function_name,
            next_args,
            task_id=task_id,
            tool_call_id=call_id,
            messages=messages,
            middleware_trace=trace,
        )

    if middleware_applied:
        return dispatch(function_args)
    result, _effective_args = run_agent_tool_execution(
        agent,
        function_name=function_name,
        function_args=function_args,
        task_id=task_id,
        tool_call_id=call_id,
        execute=dispatch,
    )
    return result


def _dispatch_agent_tool(
    agent: Any,
    function_name: str,
    function_args: dict[str, Any],
    *,
    task_id: str,
    tool_call_id: str,
    messages: Optional[list[Any]],
    middleware_trace: list[dict[str, Any]],
) -> Any:
    if function_name == "todo":
        from tools.todo_tool import todo_tool

        return todo_tool(
            todos=function_args.get("todos"),
            merge=function_args.get("merge", False),
            store=agent._todo_store,
        )

    if function_name == "session_search":
        session_db = agent._get_session_db_for_recall()
        if not session_db:
            from hermes_agent.read_models.session_recall import unavailable_message

            return json.dumps({"success": False, "error": unavailable_message()})
        from tools.session_search_tool import session_search

        return session_search(
            query=function_args.get("query", ""),
            role_filter=function_args.get("role_filter"),
            limit=function_args.get("limit", 3),
            session_id=function_args.get("session_id"),
            around_message_id=function_args.get("around_message_id"),
            window=function_args.get("window", 5),
            sort=function_args.get("sort"),
            db=session_db,
            current_session_id=agent.session_id,
        )

    if function_name == "memory":
        from tools.memory_tool import memory_tool

        target = function_args.get("target", "memory")
        result = memory_tool(
            action=function_args.get("action"),
            target=target,
            content=function_args.get("content"),
            old_text=function_args.get("old_text"),
            store=agent._memory_store,
        )
        if agent._memory_manager and function_args.get("action") in {"add", "replace"}:
            try:
                agent._memory_manager.on_memory_write(
                    function_args.get("action", ""),
                    target,
                    function_args.get("content", ""),
                    metadata=agent._build_memory_write_metadata(
                        task_id=task_id,
                        tool_call_id=tool_call_id,
                    ),
                )
            except Exception:
                pass
        return result

    if agent._context_engine_tool_names and function_name in agent._context_engine_tool_names:
        return agent.context_compressor.handle_tool_call(
            function_name,
            function_args,
            messages=messages,
        )
    if agent._memory_manager and agent._memory_manager.has_tool(function_name):
        return agent._memory_manager.handle_tool_call(function_name, function_args)
    if function_name == "clarify":
        from tools.clarify_tool import clarify_tool

        return clarify_tool(
            question=function_args.get("question", ""),
            choices=function_args.get("choices"),
            callback=agent.clarify_callback,
        )
    if function_name == "read_terminal":
        from tools.read_terminal_tool import read_terminal_tool

        return read_terminal_tool(
            start_line=function_args.get("start_line"),
            count=function_args.get("count"),
            callback=getattr(agent, "read_terminal_callback", None),
        )
    if function_name == "delegate_task":
        return agent._dispatch_delegate_task(function_args, tool_call_id=tool_call_id)

    import run_agent

    return run_agent.handle_function_call(
        function_name,
        function_args,
        task_id,
        tool_call_id=tool_call_id,
        session_id=agent.session_id or "",
        enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
        parent_agent=agent,
        enabled_toolsets=getattr(agent, "enabled_toolsets", None),
        disabled_toolsets=getattr(agent, "disabled_toolsets", None),
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
        tool_request_middleware_trace=middleware_trace,
        turn_id=getattr(agent, "_current_turn_id", "") or "",
        api_request_id=getattr(agent, "_current_api_request_id", "") or "",
    )
