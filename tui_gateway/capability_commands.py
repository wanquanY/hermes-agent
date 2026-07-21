"""Adapters for upstream capability slash commands in the TUI gateway."""

from __future__ import annotations

from typing import Any


def _prepare_moa(session_id: str, session: dict[str, Any], prompt: str) -> dict:
    from hermes_cli.config import load_config
    from hermes_cli.moa_config import moa_usage, normalize_moa_config
    from tui_gateway.core.runtime_settings import (
        _apply_model_switch,
        _snapshot_session_model_runtime,
        _stage_one_turn_model_restore,
    )

    if not prompt.strip():
        return {"type": "exec", "output": moa_usage()}

    config = load_config()
    moa = normalize_moa_config(config.get("moa") or {})
    preset = moa["default_preset"]
    if session.get("agent") is not None:
        _apply_model_switch(
            session_id,
            session,
            f"{preset} --provider moa --once",
        )
    else:
        snapshot = _snapshot_session_model_runtime(session)
        session["model_override"] = {
            "model": preset,
            "provider": "moa",
            "base_url": "moa://local",
            "api_mode": "chat_completions",
        }
        _stage_one_turn_model_restore(session, snapshot)
    return {
        "type": "send",
        "message": prompt.strip(),
        "notice": f"Mixture of Agents · {preset} · next turn only",
    }


def dispatch_capability_command(
    name: str,
    arg: str,
    *,
    session_id: str,
    session: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a command.dispatch payload, or ``None`` when not handled."""
    if name == "version":
        from hermes_cli.banner import format_banner_version_label

        return {"type": "exec", "output": format_banner_version_label()}

    if name == "learn":
        from agent.learn_prompt import build_learn_prompt

        return {
            "type": "send",
            "message": build_learn_prompt(arg),
            "notice": (
                "Learning a skill from what you described…"
                if arg.strip()
                else "Learning a skill from this conversation…"
            ),
        }

    if name == "blueprint":
        from hermes_cli.blueprint_cmd import handle_blueprint_command

        result = handle_blueprint_command(arg, surface="tui")
        if result.agent_seed:
            return {
                "type": "send",
                "message": result.agent_seed,
                "notice": result.text,
            }
        return {"type": "exec", "output": result.text}

    if name == "moa":
        if not session:
            raise ValueError("no active session for /moa")
        return _prepare_moa(session_id, session, arg)

    return None


__all__ = ["dispatch_capability_command"]
