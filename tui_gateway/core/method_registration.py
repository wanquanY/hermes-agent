"""JSON-RPC method module registration."""

from __future__ import annotations

import importlib
import sys
from typing import Any

from doxie_extension import load_extension

METHOD_MODULES = (
    "session",
    "session_branch",
    "prompt",
    "run",
    "config",
    "system",
    "paste",
    "complete",
    "model",
    "slash",
    "voice",
    "insights_rollback",
    "integrations",
    "shell",
    "workspace_artifacts",
)


def register_method_modules(target: dict[str, Any]) -> None:
    for module in METHOD_MODULES:
        name = f"tui_gateway.methods.{module}"
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    load_extension().register_gateway_methods(target)

    from tui_gateway.methods import prompt, slash, system

    for key, value in {
        "_PENDING_INPUT_COMMANDS": system._PENDING_INPUT_COMMANDS,
        "_WORKER_BLOCKED_COMMANDS": system._WORKER_BLOCKED_COMMANDS,
        "_cli_exec_blocked": system._cli_exec_blocked,
        "_mirror_slash_side_effects": slash._mirror_slash_side_effects,
        "_run_prompt_submit": prompt._run_prompt_submit,
    }.items():
        target.setdefault(key, value)
