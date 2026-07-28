from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any, Iterable


_TEAM_MISSION_GATEWAY_MODULES = (
    "hermes_team_mission.gateway.common",
    "hermes_team_mission.gateway.conversation_methods",
    "hermes_team_mission.gateway.runtime_methods",
    "hermes_team_mission.gateway.runtime_lifecycle_methods",
    "hermes_team_mission.gateway.snapshot_methods",
    "hermes_team_mission.gateway.conversation_memory_methods",
)

_TEAM_MISSION_HISTORY_MODULES = (
    "hermes_team_mission.gateway.history_methods",
)


class GatewayModuleSet:
    def __init__(self, module_names: Iterable[str]) -> None:
        object.__setattr__(
            self,
            "_modules",
            tuple(
                importlib.reload(importlib.import_module(module_name))
                for module_name in module_names
            ),
        )

    def __getattr__(self, name: str) -> Any:
        for module in self._modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_modules":
            object.__setattr__(self, name, value)
            return
        found = False
        for module in self._modules:
            if hasattr(module, name):
                setattr(module, name, value)
                found = True
        if not found:
            raise AttributeError(name)

    @property
    def modules(self) -> tuple[ModuleType, ...]:
        return self._modules


def team_mission_gateway() -> GatewayModuleSet:
    return GatewayModuleSet(_TEAM_MISSION_GATEWAY_MODULES)


def team_mission_history_gateway() -> GatewayModuleSet:
    return GatewayModuleSet(_TEAM_MISSION_HISTORY_MODULES)
