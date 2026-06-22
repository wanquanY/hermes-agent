"""Tool inheritance resolution for delegate_task subagents.

This module keeps the parent/child tool-surface rules separate from
``delegate_tool.py``'s orchestration, event, and lifecycle concerns.
"""

from __future__ import annotations

from typing import Any, List, Optional

from toolsets import TOOLSETS, get_all_toolsets, resolve_toolset, validate_toolset


EXPLICIT_NO_TOOLS_SENTINEL = "__dovie_no_tools__"
DEFAULT_BLOCKED_TOOLSETS = {
    "delegation",
    "clarify",
    "memory",
    "code_execution",
}

# Dovie profiles expose managed web research through ``dovie_web`` while
# Hermes-native prompts and models naturally ask delegate_task for ``web`` /
# ``search`` or the native ``web_search`` / ``web_extract`` tools.  Treat these
# as semantic equivalents, but still intersect with parent.valid_tool_names so a
# child never gains a tool the parent did not actually load.
SEMANTIC_TOOLSET_EQUIVALENTS = {
    "web": ["dovie_web"],
    "dovie_web": ["web"],
}

SEMANTIC_TOOL_EQUIVALENTS = {
    "search": ["serper_search_tool"],
    "web_search": ["serper_search_tool"],
    "web_extract": ["jina_web_parser_tool"],
    "serper_search_tool": ["web_search"],
    "jina_web_parser_tool": ["web_extract"],
}


def strip_blocked_toolsets(toolsets: List[str]) -> List[str]:
    """Remove toolsets whose only purpose is blocked subagent behavior."""
    return [t for t in toolsets if t not in DEFAULT_BLOCKED_TOOLSETS]


def resolve_child_tool_access(
    parent_agent,
    requested_toolsets: Optional[List[str]],
    *,
    role: str,
    blocked_tools: set[str] | frozenset[str],
    default_toolsets: List[str],
    inherit_mcp_toolsets: bool = True,
) -> tuple[List[str], List[str]]:
    """Resolve child display toolsets and exact child tool names.

    ``delegate_task.toolsets`` is model-facing and historically named for
    toolsets, but models often pass exact tool names (``search_files``,
    ``read_file``, etc.). Accept both forms, then intersect with the parent's
    real loaded ``valid_tool_names`` so the child neither loses requested tools
    nor gains sibling tools from a broader toolset.
    """
    parent_toolsets, parent_tool_names = _parent_toolsets_and_names(
        parent_agent,
        default_toolsets=default_toolsets,
    )
    requested_items = _as_text_list(requested_toolsets)

    if requested_items and all(item == EXPLICIT_NO_TOOLS_SENTINEL for item in requested_items):
        child_tool_names: set[str] = set()
        child_toolsets: List[str] = []
    elif requested_items:
        expanded_parent_toolsets = _expand_parent_toolsets(parent_toolsets)
        requested_tool_names: set[str] = set()
        requested_display_toolsets: List[str] = []
        exact_tool_display_toolsets: List[str] = []

        for item in requested_items:
            if item == EXPLICIT_NO_TOOLS_SENTINEL:
                continue
            if item in parent_tool_names:
                requested_tool_names.add(item)
                if (toolset := _toolset_for_tool(item)):
                    exact_tool_display_toolsets.append(toolset)

            resolved = _resolve_toolset_tool_names(item)
            if resolved:
                requested_tool_names.update(resolved & parent_tool_names)
                if item in expanded_parent_toolsets:
                    requested_display_toolsets.append(item)

            semantic_tool_names = _resolve_semantic_equivalent_tool_names(
                item,
                parent_tool_names,
            )
            if semantic_tool_names:
                requested_tool_names.update(semantic_tool_names)
                exact_tool_display_toolsets.extend(
                    _toolsets_for_tool_names(semantic_tool_names)
                )

        child_tool_names = requested_tool_names
        child_toolsets = _ordered_unique(
            requested_display_toolsets + exact_tool_display_toolsets
        )
        if inherit_mcp_toolsets:
            mcp_tool_names = {
                name
                for name in parent_tool_names
                if _is_mcp_toolset_name(_toolset_for_tool(name) or "")
            }
            child_tool_names.update(mcp_tool_names)
            child_toolsets = _preserve_parent_mcp_toolsets(
                child_toolsets, parent_toolsets
            )
    else:
        child_tool_names = set(parent_tool_names)
        child_toolsets = _ordered_unique(
            _toolsets_for_tool_names(child_tool_names) or sorted(parent_toolsets)
        )

    child_tool_names = _strip_blocked_tool_names(
        child_tool_names,
        role=role,
        blocked_tools=blocked_tools,
    )
    child_toolsets = strip_blocked_toolsets(child_toolsets)

    if role == "orchestrator":
        child_tool_names.add("delegate_task")
        if "delegation" not in child_toolsets:
            child_toolsets.append("delegation")

    return child_toolsets, sorted(child_tool_names)


def _as_text_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: List[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _ordered_unique(items: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _resolve_toolset_tool_names(toolset_name: str) -> set[str]:
    name = str(toolset_name or "").strip()
    if not name:
        return set()
    if validate_toolset(name):
        return set(resolve_toolset(name))
    try:
        import model_tools

        legacy = getattr(model_tools, "_LEGACY_TOOLSET_MAP", {})
        if name in legacy:
            return set(legacy[name])
    except Exception:
        return set()
    return set()


def _resolve_tool_names_from_toolsets(toolsets: set[str] | List[str]) -> set[str]:
    tool_names: set[str] = set()
    for toolset_name in toolsets:
        tool_names.update(_resolve_toolset_tool_names(str(toolset_name)))
    return tool_names


def _resolve_semantic_equivalent_tool_names(
    requested_item: str,
    parent_tool_names: set[str],
) -> set[str]:
    name = str(requested_item or "").strip()
    if not name:
        return set()

    equivalent_tools: set[str] = set()
    for toolset_name in SEMANTIC_TOOLSET_EQUIVALENTS.get(name, []):
        equivalent_tools.update(_resolve_toolset_tool_names(toolset_name))
    equivalent_tools.update(SEMANTIC_TOOL_EQUIVALENTS.get(name, []))
    return equivalent_tools & parent_tool_names


def _toolset_for_tool(tool_name: str) -> Optional[str]:
    try:
        import model_tools

        return model_tools.get_toolset_for_tool(tool_name)
    except Exception:
        return None


def _toolsets_for_tool_names(tool_names: set[str]) -> List[str]:
    return sorted(
        {
            toolset
            for name in tool_names
            if (toolset := _toolset_for_tool(name))
        }
    )


def _parent_toolsets_and_names(
    parent_agent,
    *,
    default_toolsets: List[str],
) -> tuple[set[str], set[str]]:
    parent_enabled_toolsets = _as_text_list(
        getattr(parent_agent, "enabled_toolsets", None)
    )
    parent_enabled_tools = _as_text_list(getattr(parent_agent, "enabled_tools", None))

    raw_valid_tool_names = getattr(parent_agent, "valid_tool_names", None)
    parent_tool_names: set[str] = set()
    if isinstance(raw_valid_tool_names, (set, frozenset, list, tuple)):
        parent_tool_names = {
            str(name).strip()
            for name in raw_valid_tool_names
            if str(name).strip()
        }

    if not parent_tool_names and parent_enabled_tools:
        parent_tool_names = set(parent_enabled_tools)

    parent_toolsets = set(parent_enabled_toolsets)
    if parent_tool_names:
        parent_toolsets.update(_toolsets_for_tool_names(parent_tool_names))

    if not parent_tool_names and parent_toolsets:
        parent_tool_names = _resolve_tool_names_from_toolsets(parent_toolsets)

    if not parent_toolsets and parent_tool_names:
        parent_toolsets = set(_toolsets_for_tool_names(parent_tool_names))

    if not parent_toolsets and not parent_tool_names:
        parent_toolsets = set(default_toolsets)
        parent_tool_names = _resolve_tool_names_from_toolsets(parent_toolsets)

    return parent_toolsets, parent_tool_names


def _strip_blocked_tool_names(
    tool_names: set[str],
    *,
    role: str,
    blocked_tools: set[str] | frozenset[str],
) -> set[str]:
    blocked = set(blocked_tools)
    if role == "orchestrator":
        blocked.discard("delegate_task")
    return {name for name in tool_names if name not in blocked}


def _is_mcp_toolset_name(name: str) -> bool:
    if not name:
        return False
    if str(name).startswith("mcp-"):
        return True
    try:
        from tools.registry import registry

        target = registry.get_toolset_alias_target(str(name))
    except Exception:
        target = None
    return bool(target and str(target).startswith("mcp-"))


def _expand_parent_toolsets(parent_toolsets: set) -> set:
    parent_tool_names: set = set()
    for ts_name in parent_toolsets:
        try:
            parent_tool_names.update(resolve_toolset(str(ts_name)))
        except Exception:
            ts_def = TOOLSETS.get(ts_name)
            if ts_def:
                parent_tool_names.update(ts_def.get("tools", []))

    if not parent_tool_names:
        return set(parent_toolsets)

    expanded = set(parent_toolsets)
    for ts_name, ts_def in get_all_toolsets().items():
        if ts_name in expanded:
            continue
        ts_tools = ts_def.get("tools", [])
        if ts_tools and set(ts_tools).issubset(parent_tool_names):
            expanded.add(ts_name)
    return expanded


def _preserve_parent_mcp_toolsets(
    child_toolsets: List[str],
    parent_toolsets: set[str],
) -> List[str]:
    preserved = list(child_toolsets)
    for toolset_name in sorted(parent_toolsets):
        if _is_mcp_toolset_name(toolset_name) and toolset_name not in preserved:
            preserved.append(toolset_name)
    return preserved
