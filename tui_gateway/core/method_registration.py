"""JSON-RPC method module registration."""

from __future__ import annotations

import importlib
import sys
from typing import Any

MODULES = (
    "session",
    "session_history",
    "session_interrupt",
    "session_runtime_controls",
    "runtime_cloud_proxy",
    "live_session",
    "session_branch",
    "conversation_activity",
    "activity",
    "dispatch",
    "conversation_render_snapshot",
    "prompt",
    "prompt_respond",
    "run",
    "team_registry",
    "profile_registry",
    "hermes_team_mission.gateway.common",
    "hermes_team_mission.gateway.conversation_methods",
    "hermes_team_mission.gateway.runtime_methods",
    "hermes_team_mission.gateway.runtime_lifecycle_methods",
    "hermes_team_mission.gateway.snapshot_methods",
    "hermes_team_mission.gateway.conversation_memory_methods",
    "hermes_team_mission.gateway.history_methods",
    "config",
    "setup",
    "system",
    "process",
    "handoff",
    "billing",
    "paste",
    "complete",
    "codex",
    "model",
    "slash",
    "voice",
    "insights_rollback",
    "integrations",
    "shell",
    "workspace_artifacts",
)


def register_method_modules(target: dict[str, Any]) -> None:
    for module in MODULES:
        name = module if "." in module else f"tui_gateway.methods.{module}"
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    from tui_gateway.methods import prompt, slash, system

    for key, value in {
        "_PENDING_INPUT_COMMANDS": system._PENDING_INPUT_COMMANDS,
        "_WORKER_BLOCKED_COMMANDS": system._WORKER_BLOCKED_COMMANDS,
        "_cli_exec_blocked": system._cli_exec_blocked,
        "_mirror_slash_side_effects": slash._mirror_slash_side_effects,
        "_run_prompt_submit": prompt._run_prompt_submit,
    }.items():
        target.setdefault(key, value)
