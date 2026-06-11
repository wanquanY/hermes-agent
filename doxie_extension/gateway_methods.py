"""Gateway method ownership for the Doxie Hermes extension."""

from __future__ import annotations

import importlib
import sys
from typing import Any

DOXIE_GATEWAY_METHOD_MODULES = (
    "tui_gateway.methods.system",
    "tui_gateway.methods.session",
    "tui_gateway.methods.session_branch",
    "tui_gateway.methods.run",
    "tui_gateway.methods.team_mission",
    "tui_gateway.methods.team_mission_history",
    "tui_gateway.methods.model",
    "tui_gateway.methods.prompt",
    "tui_gateway.methods.integrations",
    "tui_gateway.methods.workspace_artifacts",
)

DOXIE_GATEWAY_METHOD_OVERRIDES = frozenset(
    {
        "approval.pending.list",
        "approval.policy.get",
        "approval.policy.set",
        "approval.respond",
        "artifacts.list",
        "clarify.respond",
        "cron.manage",
        "events.compact",
        "events.prune",
        "events.subscribe",
        "events.unsubscribe",
        "gateway.capabilities",
        "model.set",
        "platforms.manage",
        "profile.prepare_runtime",
        "prompt.submit",
        "run.cancel",
        "run.events",
        "run.fail",
        "run.list",
        "run.reserve",
        "run.status",
        "run.submit",
        "runtime.ensure",
        "runtime.status",
        "secret.respond",
        "session.create",
        "session.branch",
        "session.delete",
        "session.list",
        "session.messages",
        "session.message_metadata.merge",
        "session.status",
        "session.title",
        "session.usage",
        "skills.list",
        "skills.manage",
        "sudo.respond",
        "team_mission.create",
        "team_capability.snapshot.bind",
        "team_capability.snapshot.get",
        "team_capability.snapshot.refresh",
        "team_mission.edge.create",
        "team_mission.events",
        "team_mission.graph",
        "team_mission.graph.reduce",
        "team_mission.conversation.ensure",
        "team_mission.conversation.resolve",
        "team_mission.conversation.list",
        "team_mission.conversation.rename",
        "team_mission.conversation.delete",
        "team_mission.cancel",
        "team_mission.memory.compile",
        "team_mission.memory.delete",
        "team_mission.memory.events",
        "team_mission.memory.list",
        "team_mission.memory.pack",
        "team_mission.memory.slice",
        "team_mission.memory.update",
        "team_mission.message.submit",
        "team_mission.node.create",
        "team_mission.node.bind_run",
        "team_mission.node.history",
        "team_mission.node.start",
        "team_mission.node.update",
        "team_mission.plan.complete",
        "team_mission.schedule.ready",
        "team_mission.subscribe",
        "team_mission.team_profile.get",
        "toolsets.list",
        "workspace.current",
        "workspace.list",
    }
)


def doxie_gateway_method_overrides() -> frozenset[str]:
    return DOXIE_GATEWAY_METHOD_OVERRIDES


def register_gateway_methods(_registry: dict[str, Any] | None = None) -> None:
    """Load Doxie Gateway method modules through the extension boundary."""
    for module_name in DOXIE_GATEWAY_METHOD_MODULES:
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
