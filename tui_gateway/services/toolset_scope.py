"""Turn-scoped toolset management for TUI gateway sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def normalize_enabled_toolsets(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = [raw]

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def default_public_toolsets() -> list[str]:
    from toolsets import get_all_toolsets, is_internal_toolset

    return [
        name
        for name in sorted(get_all_toolsets().keys())
        if not is_internal_toolset(name)
    ]


def merge_enabled_toolsets(base: list[str] | None, extra: list[str]) -> list[str] | None:
    if not extra:
        return base
    merged = default_public_toolsets() if base is None else list(base)
    seen = set(merged)
    for name in extra:
        if name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


def refresh_agent_tool_filter(agent: Any, enabled_toolsets: list[str] | None) -> None:
    from model_tools import get_tool_definitions

    disabled_toolsets = getattr(agent, "disabled_toolsets", None)
    agent.enabled_toolsets = enabled_toolsets
    agent.tools = get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=getattr(agent, "quiet_mode", True),
    )
    agent.valid_tool_names = {
        tool["function"]["name"]
        for tool in agent.tools or []
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }


def ensure_session_turn_toolsets(
    *,
    sid: str,
    session: dict[str, Any],
    requested_toolsets: Any,
    load_enabled_toolsets: Callable[[], list[str] | None],
    emit_session_info: Callable[[str, Any], None],
) -> None:
    """Merge run-scoped toolsets into a warm session's effective tool surface."""
    extras = normalize_enabled_toolsets(requested_toolsets)
    if not extras:
        return

    from toolsets import validate_toolset

    valid_extras = [name for name in extras if validate_toolset(name)]
    if not valid_extras:
        return

    base = session.get("enabled_toolsets_override")
    if base is None:
        agent = session.get("agent")
        base = (
            getattr(agent, "enabled_toolsets", None)
            if agent is not None
            else load_enabled_toolsets()
        )
    effective = merge_enabled_toolsets(base, valid_extras)
    session["enabled_toolsets_override"] = effective

    agent = session.get("agent")
    if agent is None or getattr(agent, "enabled_toolsets", None) == effective:
        return

    refresh_agent_tool_filter(agent, effective)
    emit_session_info(sid, agent)
