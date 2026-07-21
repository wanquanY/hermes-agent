"""Conversation-boundary restoration for the classic CLI runtime."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from hermes_cli.session_scope import parse_service_tier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliModelRuntime:
    model: str
    provider: str
    base_url: str
    api_key: Any
    api_mode: str


def capture_initial_model_runtime(cli: Any) -> CliModelRuntime:
    """Capture the effective startup route for config-without-model fallback."""
    return CliModelRuntime(
        model=str(getattr(cli, "model", "") or ""),
        provider=str(getattr(cli, "provider", "") or ""),
        base_url=str(getattr(cli, "base_url", "") or ""),
        api_key=getattr(cli, "api_key", ""),
        api_mode=str(getattr(cli, "api_mode", "") or ""),
    )


def _configured_model(config: dict, fallback: CliModelRuntime) -> tuple[str, str]:
    model_cfg = config.get("model", {}) if isinstance(config, dict) else {}
    if isinstance(model_cfg, dict):
        return (
            str(model_cfg.get("default") or model_cfg.get("model") or fallback.model),
            str(model_cfg.get("provider") or fallback.provider),
        )
    return str(model_cfg or fallback.model), fallback.provider


def _apply_model_result(cli: Any, result: Any) -> None:
    cli.model = result.new_model
    cli.provider = result.target_provider
    cli.requested_provider = result.target_provider
    cli._explicit_api_key = result.api_key
    cli._explicit_base_url = result.base_url
    if result.api_key:
        cli.api_key = result.api_key
    if result.base_url:
        cli.base_url = result.base_url
    if result.api_mode:
        cli.api_mode = result.api_mode


def _restore_model(cli: Any, config: dict, fallback: CliModelRuntime) -> None:
    target_model, target_provider = _configured_model(config, fallback)
    if not target_model:
        return
    current_model = str(getattr(cli, "model", "") or "")
    current_provider = str(getattr(cli, "provider", "") or "")
    if target_model == current_model and target_provider == current_provider:
        return
    try:
        from hermes_cli.model_switch import switch_model

        result = switch_model(
            raw_input=target_model,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=str(getattr(cli, "base_url", "") or ""),
            current_api_key=getattr(cli, "api_key", ""),
            is_global=False,
            explicit_provider=target_provider,
        )
        if not result.success:
            logger.warning("Could not restore configured model on /new: %s", result.error_message)
            return
        agent = getattr(cli, "agent", None)
        if agent is not None and hasattr(agent, "switch_model"):
            agent.switch_model(
                new_model=result.new_model,
                new_provider=result.target_provider,
                api_key=result.api_key,
                base_url=result.base_url,
                api_mode=result.api_mode,
            )
        _apply_model_result(cli, result)
    except Exception:
        logger.debug("/new model restoration failed", exc_info=True)


def reset_cli_session_runtime(cli: Any, config: dict) -> None:
    """Restore model, reasoning, and fast mode at a conversation boundary."""
    fallback = getattr(cli, "_initial_model_runtime", None)
    if not isinstance(fallback, CliModelRuntime):
        fallback = capture_initial_model_runtime(cli)
        cli._initial_model_runtime = fallback

    cli._pending_one_turn_model_restore = None
    _restore_model(cli, config, fallback)

    from hermes_constants import resolve_reasoning_config

    reasoning_config = resolve_reasoning_config(config, getattr(cli, "model", ""))
    agent_cfg = config.get("agent", {}) if isinstance(config, dict) else {}
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    service_tier = parse_service_tier(agent_cfg.get("service_tier", ""))
    cli.reasoning_config = reasoning_config
    cli.service_tier = service_tier

    agent = getattr(cli, "agent", None)
    if agent is None:
        return
    agent.reasoning_config = reasoning_config
    agent.service_tier = service_tier
    request_overrides = dict(getattr(agent, "request_overrides", {}) or {})
    request_overrides.pop("service_tier", None)
    request_overrides.pop("speed", None)
    if service_tier == "priority":
        try:
            from hermes_cli.models import resolve_fast_mode_overrides

            request_overrides.update(
                resolve_fast_mode_overrides(getattr(cli, "model", "")) or {}
            )
        except Exception:
            logger.debug("Could not restore fast-mode request overrides", exc_info=True)
    agent.request_overrides = request_overrides
