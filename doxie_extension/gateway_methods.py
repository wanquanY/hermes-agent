"""Gateway method ownership for the Doxie Hermes extension."""

from __future__ import annotations

import importlib
import sys
from typing import Any

DOXIE_GATEWAY_METHOD_MODULES = (
    "tui_gateway.methods.system",
    "tui_gateway.methods.session",
    "tui_gateway.methods.run",
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
        "runtime.status",
        "secret.respond",
        "session.create",
        "session.delete",
        "session.list",
        "session.messages",
        "session.status",
        "session.title",
        "session.usage",
        "skills.list",
        "skills.manage",
        "sudo.respond",
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
