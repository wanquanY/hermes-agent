"""Configuration policy for routing dashboard turns through run workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils import is_truthy_value


@dataclass(frozen=True)
class DashboardProcessIsolation:
    """Normalized dashboard isolation settings.

    The local architecture uses the canonical run-worker supervisor instead of
    upstream's second compute-host process family.  The public
    ``dashboard.turn_isolation`` switch remains compatible.
    """

    turn_isolation: bool = False


def load_dashboard_process_isolation(
    config: dict[str, Any] | None = None,
) -> DashboardProcessIsolation:
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    dashboard = config.get("dashboard") if isinstance(config, dict) else {}
    dashboard = dashboard if isinstance(dashboard, dict) else {}
    return DashboardProcessIsolation(
        turn_isolation=is_truthy_value(
            dashboard.get("turn_isolation"),
            default=False,
        )
    )


__all__ = ["DashboardProcessIsolation", "load_dashboard_process_isolation"]
