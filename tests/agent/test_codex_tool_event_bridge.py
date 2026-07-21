from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from agent.codex_runtime import _forward_codex_tool_event


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        tool_start_callback=Mock(),
        tool_complete_callback=Mock(),
    )


def test_internal_mcp_tool_progress_uses_bare_native_name() -> None:
    agent = _agent()
    item = {
        "type": "mcpToolCall",
        "id": "mcp-1",
        "server": "hermes-tools",
        "tool": "browser_navigate",
        "arguments": {"url": "https://example.com"},
    }

    _forward_codex_tool_event(agent, {"method": "item/started", "params": {"item": item}})

    assert agent.tool_start_callback.call_args.args[1] == "browser_navigate"


def test_builtin_web_search_emits_progress_pair() -> None:
    agent = _agent()
    item = {
        "type": "webSearch",
        "id": "search-1",
        "query": "Hermes upstream",
    }

    _forward_codex_tool_event(agent, {"method": "item/started", "params": {"item": item}})
    _forward_codex_tool_event(agent, {"method": "item/completed", "params": {"item": item}})

    assert agent.tool_start_callback.call_args.args[1] == "web_search"
    assert agent.tool_complete_callback.call_args.args[1] == "web_search"
