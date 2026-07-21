"""Canonical gateway-to-agent filesystem checkpoint configuration."""

from __future__ import annotations

from typing import Any

from hermes_agent.gateway.runtime_config import load_gateway_runtime_config
from hermes_cli.config import DEFAULT_CONFIG
from hermes_constants import get_hermes_home, get_hermes_home_override


def _runtime_home():
    return get_hermes_home_override() or get_hermes_home()


def _positive_int(value: Any, default: int, *, allow_zero: bool = False) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    minimum = 0 if allow_zero else 1
    return parsed if parsed >= minimum else default


def checkpoint_agent_kwargs(config: dict | None = None) -> dict[str, int | bool]:
    """Translate gateway YAML into the stable ``AIAgent`` checkpoint contract.

    ``checkpoints: true`` remains supported for old installations while the
    mapping form owns all resource limits. Missing or malformed values fall
    back to the central CLI defaults instead of diverging per runtime entry.
    """
    if config is None:
        config = load_gateway_runtime_config(_runtime_home())

    defaults = DEFAULT_CONFIG["checkpoints"]
    raw = config.get("checkpoints", {}) if isinstance(config, dict) else {}
    if isinstance(raw, bool):
        enabled = raw
        raw = {}
    elif isinstance(raw, dict):
        enabled = bool(raw.get("enabled", defaults["enabled"]))
    else:
        enabled = bool(defaults["enabled"])
        raw = {}

    return {
        "checkpoints_enabled": enabled,
        "checkpoint_max_snapshots": _positive_int(
            raw.get("max_snapshots"), int(defaults["max_snapshots"])
        ),
        "checkpoint_max_total_size_mb": _positive_int(
            raw.get("max_total_size_mb"),
            int(defaults["max_total_size_mb"]),
            allow_zero=True,
        ),
        "checkpoint_max_file_size_mb": _positive_int(
            raw.get("max_file_size_mb"),
            int(defaults["max_file_size_mb"]),
            allow_zero=True,
        ),
    }


__all__ = ["checkpoint_agent_kwargs"]
