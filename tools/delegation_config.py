"""Delegation configuration resolution shared by execution components."""

from __future__ import annotations

from typing import Any


def load_delegation_config() -> dict[str, Any]:
    """Resolve runtime delegation config before falling back to persisted config."""
    try:
        from cli import CLI_CONFIG

        runtime = CLI_CONFIG.get("delegation") or {}
        if runtime:
            return dict(runtime)
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        persisted = load_config().get("delegation") or {}
        return dict(persisted) if isinstance(persisted, dict) else {}
    except Exception:
        return {}
