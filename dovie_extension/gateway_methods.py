"""Gateway method ownership for the Dovie Hermes extension."""

from __future__ import annotations

import importlib
import sys
from typing import Any

MODULES = (
    "tui_gateway.methods.system",
    "tui_gateway.methods.session",
    "tui_gateway.methods.session_branch",
    "tui_gateway.methods.conversation_activity",
    "tui_gateway.methods.conversation_render_snapshot",
    "tui_gateway.methods.run",
    "tui_gateway.methods.team_registry",
    "tui_gateway.methods.profile_registry",
    "hermes_team_mission.gateway.common",
    "hermes_team_mission.gateway.conversation_methods",
    "hermes_team_mission.gateway.runtime_methods",
    "hermes_team_mission.gateway.snapshot_methods",
    "hermes_team_mission.gateway.memory_methods",
    "hermes_team_mission.gateway.history_methods",
    "tui_gateway.methods.model",
    "tui_gateway.methods.prompt",
    "tui_gateway.methods.integrations",
    "tui_gateway.methods.workspace_artifacts",
)

def dovie_gateway_method_overrides() -> frozenset[str]:
    return frozenset()


def register_gateway_methods(_registry: dict[str, Any] | None = None) -> None:
    """Load Dovie Gateway method modules through the extension boundary."""
    for module_name in MODULES:
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
