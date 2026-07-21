"""Schema-aware policy for ``hermes config set`` dotted keys.

Runtime configuration intentionally supports both closed dictionaries (for
example ``approvals``) and open dictionaries whose child names are supplied by
users (for example ``providers`` and platform maps).  This module owns that
distinction without coupling the command writer to the full config loader.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Mapping


_OPEN_ROOTS = frozenset(
    {
        "providers",
        "credential_pool_strategies",
        "mcp_servers",
        "hooks",
        "quick_commands",
        "personalities",
        "command_allowlist",
        "model_catalog",
        "channel_prompts",
        "server_actions",
        "secrets",
        "goals",
        "custom_providers",
        "platform_toolsets",
        "profile_routes",
        "multiplex_profiles",
    }
)

_EXTENSIBLE_ROOTS = frozenset(
    {
        "discord",
        "telegram",
        "slack",
        "whatsapp",
        "signal",
        "mattermost",
        "matrix",
        "feishu",
        "wecom",
        "weixin",
        "bluebubbles",
        "qqbot",
        "yuanbao",
        "email",
        "sms",
        "dingtalk",
        "sessions",
        "checkpoints",
    }
)

_DYNAMIC_CONTAINER_NAMES = frozenset({"platforms"})
_MISSING = object()


@dataclass(frozen=True)
class ConfigKeyValidation:
    """Result of checking one dotted key against the running schema."""

    known: bool
    suggestion: str | None = None


def default_leaf_value(defaults: Mapping[str, Any], dotted_key: str) -> Any:
    """Return a declared scalar default, or a private missing sentinel."""
    node: Any = defaults
    for part in dotted_key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return _MISSING if isinstance(node, Mapping) else node


def key_declares_string(defaults: Mapping[str, Any], dotted_key: str) -> bool:
    """Whether the schema declares *dotted_key* as a string leaf."""
    return isinstance(default_leaf_value(defaults, dotted_key), str)


def _closest(value: str, candidates: set[str]) -> str | None:
    matches = difflib.get_close_matches(
        value,
        sorted(candidates),
        n=1,
        cutoff=0.6,
    )
    return matches[0] if matches else None


def validate_config_key(
    dotted_key: str,
    *,
    defaults: Mapping[str, Any],
    extra_known_roots: set[str] | frozenset[str] = frozenset(),
) -> ConfigKeyValidation:
    """Validate as deeply as the non-extensible portion of the schema allows."""
    if not dotted_key:
        return ConfigKeyValidation(False)

    segments = dotted_key.split(".")
    top = segments[0]
    if top.startswith("_"):
        return ConfigKeyValidation(True)

    known_roots = set(defaults) | set(extra_known_roots) | set(_OPEN_ROOTS)
    known_roots |= set(_EXTENSIBLE_ROOTS) | set(_DYNAMIC_CONTAINER_NAMES)

    if top not in known_roots:
        suggestion = _closest(top, known_roots)
        if suggestion and len(segments) > 1:
            suggestion = ".".join([suggestion, *segments[1:]])
        return ConfigKeyValidation(False, suggestion)

    if (
        top in _OPEN_ROOTS
        or top in _EXTENSIBLE_ROOTS
        or top in _DYNAMIC_CONTAINER_NAMES
    ):
        return ConfigKeyValidation(True)

    node: Any = defaults.get(top)
    consumed = [top]
    for segment in segments[1:]:
        if segment in _DYNAMIC_CONTAINER_NAMES:
            return ConfigKeyValidation(True)
        if not isinstance(node, Mapping):
            # Preserve the historical ability to replace a scalar with a
            # deeper user-defined structure.
            return ConfigKeyValidation(True)
        if segment not in node:
            suggestion = _closest(segment, set(node))
            if suggestion:
                suggestion = ".".join([*consumed, suggestion])
            return ConfigKeyValidation(False, suggestion)
        consumed.append(segment)
        node = node[segment]

    return ConfigKeyValidation(True)
