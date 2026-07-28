#!/usr/bin/env python3
"""List the interactive terminal tabs owned by the current desktop conversation."""

import json
from typing import Callable, Optional

from tools.registry import registry, tool_error
from utils import env_var_enabled


def list_terminals_tool(callback: Optional[Callable] = None) -> str:
    if callback is None:
        return tool_error("list_terminals is only available in the Hermes desktop app.")
    try:
        raw = callback()
    except Exception as exc:
        return tool_error(f"Failed to list terminals: {exc}")
    if not raw:
        return json.dumps({"terminals": [], "selected_terminal_id": ""})
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)
    except (TypeError, ValueError):
        return tool_error("Desktop returned an invalid terminal list response.")


def check_list_terminals_requirements() -> bool:
    return env_var_enabled("HERMES_DESKTOP")


LIST_TERMINALS_SCHEMA = {
    "name": "list_terminals",
    "description": (
        "List the interactive terminal tabs currently open beside this conversation. "
        "Returns each opaque terminal_id, title, cwd, process status, last activity time, "
        "and whether the tab is selected in the UI. Every terminal with status='running' "
        "continues running independently, including unselected tabs; selected only means "
        "currently visible and does not indicate whether a terminal is alive. Use this "
        "before read_terminal or write_terminal whenever more than one terminal may be open."
    ),
    "parameters": {"type": "object", "properties": {}},
}


registry.register(
    name="list_terminals",
    toolset="terminal",
    schema=LIST_TERMINALS_SCHEMA,
    handler=lambda _args, **kw: list_terminals_tool(callback=kw.get("callback")),
    check_fn=check_list_terminals_requirements,
    emoji="🖥️",
)
