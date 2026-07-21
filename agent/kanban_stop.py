"""Policy guard preventing kanban workers from exiting without a board outcome."""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})


def kanban_stop_nudge_enabled() -> bool:
    configured = os.getenv("HERMES_KANBAN_STOP_NUDGE")
    if configured and configured.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(os.getenv("HERMES_KANBAN_TASK", "").strip())


def _tool_call_name(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(tool_call.get("name") or "")
    function = getattr(tool_call, "function", None)
    if function is not None:
        return str(getattr(function, "name", "") or "")
    return str(getattr(tool_call, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            if any(
                _tool_call_name(call) in _TERMINAL_KANBAN_TOOLS
                for call in message.get("tool_calls") or ()
            ):
                return True
        elif (
            message.get("role") == "tool"
            and str(message.get("name") or "") in _TERMINAL_KANBAN_TOOLS
        ):
            return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = 2,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a bounded continuation requirement for a non-terminal worker."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None
    task = (task_id or os.getenv("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{task}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
