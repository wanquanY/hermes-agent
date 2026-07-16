"""Canonical identity rules for Model Context Protocol tools.

All new MCP tool names use ``mcp__<server>__<tool>``.  The double delimiter
is part of the wire contract; legacy single-underscore names are accepted only
through :func:`resolve_legacy_mcp_tool_name` and are never emitted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MCP_TOOL_PREFIX = "mcp__"
MCP_TOOL_DELIMITER = "__"
LEGACY_MCP_TOOL_PREFIX = "mcp_"


def sanitize_mcp_name_component(value: object) -> str:
    """Return a provider-safe, delimiter-safe MCP name component.

    Consecutive separators collapse to one underscore so ``__`` remains an
    unambiguous boundary in the canonical wire name.
    """

    component = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_")
    return component or "unnamed"


def canonical_mcp_tool_name(server_name: object, tool_name: object) -> str:
    """Build the only MCP tool-name form that new code may write."""

    server = sanitize_mcp_name_component(server_name)
    tool = sanitize_mcp_name_component(tool_name)
    return f"{MCP_TOOL_PREFIX}{server}{MCP_TOOL_DELIMITER}{tool}"


def split_canonical_mcp_tool_name(name: object) -> tuple[str, str] | None:
    """Return ``(server, tool)`` for a canonical name, otherwise ``None``."""

    value = str(name or "")
    if not value.startswith(MCP_TOOL_PREFIX):
        return None
    payload = value[len(MCP_TOOL_PREFIX):]
    if MCP_TOOL_DELIMITER not in payload:
        return None
    server, tool = payload.split(MCP_TOOL_DELIMITER, 1)
    if not server or not tool or MCP_TOOL_DELIMITER in server or MCP_TOOL_DELIMITER in tool:
        return None
    return server, tool


def is_canonical_mcp_tool_name(name: object) -> bool:
    return split_canonical_mcp_tool_name(name) is not None


def is_mcp_tool_name(name: object) -> bool:
    """Recognize canonical and legacy MCP names at read boundaries."""

    value = str(name or "")
    return is_canonical_mcp_tool_name(value) or (
        value.startswith(LEGACY_MCP_TOOL_PREFIX)
        and not value.startswith(MCP_TOOL_PREFIX)
    )


def legacy_mcp_tool_name(server_name: object, tool_name: object) -> str:
    """Build a legacy name for migration comparison only.

    This function must never be used for schema, registry, transcript, or
    transport writes.
    """

    server = sanitize_mcp_name_component(server_name)
    tool = sanitize_mcp_name_component(tool_name)
    return f"{LEGACY_MCP_TOOL_PREFIX}{server}_{tool}"


def resolve_legacy_mcp_tool_name(
    name: object,
    canonical_names: Iterable[str],
) -> str | None:
    """Resolve one legacy name against known canonical registry entries.

    The old representation is intrinsically ambiguous when underscores occur
    in either component.  Known canonical entries provide the missing context.
    Exactly one match is required; zero or multiple matches fail closed.
    """

    value = str(name or "")
    known = tuple(dict.fromkeys(str(item) for item in canonical_names))
    if is_canonical_mcp_tool_name(value):
        return value if value in set(known) else None
    if not value.startswith(LEGACY_MCP_TOOL_PREFIX):
        return None

    matches: list[str] = []
    for candidate in known:
        split = split_canonical_mcp_tool_name(candidate)
        if split is None:
            continue
        if legacy_mcp_tool_name(*split) == value:
            matches.append(candidate)
    unique = tuple(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def to_anthropic_oauth_wire_name(name: object) -> str:
    """Normalize a Hermes tool name for Anthropic's OAuth wire convention."""

    value = str(name or "")
    if value.startswith(MCP_TOOL_PREFIX):
        return value
    if value.startswith(LEGACY_MCP_TOOL_PREFIX):
        return MCP_TOOL_PREFIX + value[len(LEGACY_MCP_TOOL_PREFIX):]
    return MCP_TOOL_PREFIX + value


def from_anthropic_oauth_wire_name(
    name: object,
    registered_names: Iterable[str],
) -> str:
    """Map an OAuth wire name back to one registered Hermes identity.

    Canonical native MCP names win unchanged.  Bare Hermes tools use the
    payload after ``mcp__``.  Replayed legacy MCP names are resolved through
    the same known-registry migration rule and are returned canonical.
    """

    value = str(name or "")
    known = tuple(dict.fromkeys(str(item) for item in registered_names))
    known_set = set(known)
    if value in known_set:
        return value
    if not value.startswith(MCP_TOOL_PREFIX):
        return value
    payload = value[len(MCP_TOOL_PREFIX):]
    if payload in known_set:
        return payload
    legacy = LEGACY_MCP_TOOL_PREFIX + payload
    migrated = resolve_legacy_mcp_tool_name(legacy, known)
    return migrated or payload
