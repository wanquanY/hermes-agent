#!/usr/bin/env python3
"""Write input to one existing interactive terminal tab in the desktop GUI."""

import json
from typing import Callable, Optional

from tools.registry import registry, tool_error
from utils import env_var_enabled


MAX_INPUT_CHARS = 32_768


def write_terminal_tool(
    terminal_id: str,
    input_text: str,
    *,
    submit: bool = True,
    reveal: bool = True,
    callback: Optional[Callable] = None,
) -> str:
    terminal = str(terminal_id or "").strip()
    if not terminal:
        return tool_error("terminal_id is required. Call list_terminals first.")
    if callback is None:
        return tool_error("write_terminal is only available in the Hermes desktop app.")
    data = str(input_text or "")
    if not data and not submit:
        return tool_error("input must not be empty when submit is false.")
    if len(data) > MAX_INPUT_CHARS:
        return tool_error(f"input exceeds the {MAX_INPUT_CHARS}-character limit.")
    if submit:
        data += "\r"
    try:
        raw = callback(terminal, data, reveal)
    except Exception as exc:
        return tool_error(f"Failed to write terminal: {exc}")
    if not raw:
        return tool_error("No matching in-app terminal is open, or the write timed out.")
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return json.dumps({"terminal_id": terminal, "status": str(raw)}, ensure_ascii=False)
    if isinstance(result, dict) and result.get("ok") is False:
        return tool_error(str(result.get("error") or "Desktop rejected the terminal write."))
    return json.dumps(result, ensure_ascii=False)


def check_write_terminal_requirements() -> bool:
    return env_var_enabled("HERMES_DESKTOP")


WRITE_TERMINAL_SCHEMA = {
    "name": "write_terminal",
    "description": (
        "Write input into an existing interactive terminal tab beside this chat. "
        "Always call list_terminals and read_terminal first so you target the correct "
        "terminal and confirm it is ready for input. The command is sent to the exact "
        "existing PTY; output appears live in that tab."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "terminal_id": {
                "type": "string",
                "description": "Opaque terminal_id returned by list_terminals.",
            },
            "input": {
                "type": "string",
                "description": "Command or interactive input to send to the terminal.",
            },
            "submit": {
                "type": "boolean",
                "description": "Append Enter after input. Defaults to true.",
                "default": True,
            },
            "reveal": {
                "type": "boolean",
                "description": "Reveal the target tab so the user sees the live execution. Defaults to true.",
                "default": True,
            },
        },
        "required": ["terminal_id", "input"],
    },
}


registry.register(
    name="write_terminal",
    toolset="terminal",
    schema=WRITE_TERMINAL_SCHEMA,
    handler=lambda args, **kw: write_terminal_tool(
        terminal_id=args.get("terminal_id", ""),
        input_text=args.get("input", ""),
        submit=args.get("submit", True) is not False,
        reveal=args.get("reveal", True) is not False,
        callback=kw.get("callback"),
    ),
    check_fn=check_write_terminal_requirements,
    emoji="⌨️",
)
