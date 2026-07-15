"""Gateway runtime configuration helpers.

These helpers are shared by the long-lived gateway runner and request/response
channel adapters such as the API server. They intentionally avoid importing
``hermes_gateway.runner`` so channel code can resolve model/provider settings
without coupling to the runner coordinator.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_home, parse_reasoning_effort

logger = logging.getLogger(__name__)


def _config_path(hermes_home: Path | None = None) -> Path:
    return Path(hermes_home or get_hermes_home()) / "config.yaml"


def load_gateway_runtime_config(hermes_home: Path | None = None) -> dict:
    """Load and parse ``config.yaml``, returning ``{}`` on any error."""
    config_path = _config_path(hermes_home)
    try:
        from hermes_cli.config import get_config_path, read_raw_config

        if config_path == get_config_path():
            return read_raw_config()
    except Exception as exc:
        logger.debug("failed to load gateway runtime config via hermes_cli config: %s", exc)

    try:
        if config_path.exists():
            import yaml

            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        logger.debug("Could not load gateway config from %s", config_path)
    return {}


def resolve_gateway_model(config: dict | None = None) -> str:
    """Read the configured default model from ``config.yaml``."""
    cfg = config if config is not None else load_gateway_runtime_config()
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, str):
        return model_cfg
    if isinstance(model_cfg, dict):
        return model_cfg.get("default") or model_cfg.get("model") or ""
    return ""


def _try_resolve_fallback_provider(hermes_home: Path | None = None) -> dict | None:
    """Attempt to resolve credentials from fallback provider config."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    try:
        cfg = load_gateway_runtime_config(hermes_home)
        fb = cfg.get("fallback_providers") or cfg.get("fallback_model")
        if not fb:
            return None
        fb_list = fb if isinstance(fb, list) else [fb]
        for entry in fb_list:
            if not isinstance(entry, dict):
                continue
            try:
                runtime = resolve_runtime_provider(
                    requested=entry.get("provider"),
                    explicit_base_url=entry.get("base_url"),
                    explicit_api_key=entry.get("api_key"),
                )
                logger.info(
                    "Fallback provider resolved: %s model=%s",
                    runtime.get("provider"),
                    entry.get("model"),
                )
                return {
                    "api_key": runtime.get("api_key"),
                    "base_url": runtime.get("base_url"),
                    "provider": runtime.get("provider"),
                    "api_mode": runtime.get("api_mode"),
                    "command": runtime.get("command"),
                    "args": list(runtime.get("args") or []),
                    "credential_pool": runtime.get("credential_pool"),
                    "model": entry.get("model"),
                }
            except Exception as fb_exc:
                logger.debug("Fallback entry %s failed: %s", entry.get("provider"), fb_exc)
                continue
    except Exception as exc:
        logger.debug("failed to resolve fallback runtime config: %s", exc)
    return None


def resolve_runtime_agent_kwargs(hermes_home: Path | None = None) -> dict:
    """Resolve provider credentials for gateway-created AIAgent instances."""
    from hermes_cli.auth import AuthError
    from hermes_cli.runtime_provider import (
        format_runtime_provider_error,
        resolve_runtime_provider,
    )

    try:
        runtime = resolve_runtime_provider(
            requested=os.getenv("HERMES_INFERENCE_PROVIDER"),
        )
    except AuthError as auth_exc:
        logger.warning("Primary provider auth failed: %s; trying fallback", auth_exc)
        fb_config = _try_resolve_fallback_provider(hermes_home)
        if fb_config is not None:
            return fb_config
        raise RuntimeError(format_runtime_provider_error(auth_exc)) from auth_exc
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool"),
    }


def load_reasoning_config(config: dict | None = None) -> dict | None:
    """Load and parse ``agent.reasoning_effort`` from config."""
    cfg = config if config is not None else load_gateway_runtime_config()
    effort = str(cfg_get(cfg, "agent", "reasoning_effort", default="") or "").strip()
    result = parse_reasoning_effort(effort)
    if effort and result is None:
        logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
    return result


def load_fallback_model(config: dict | None = None) -> list | dict | None:
    """Load fallback provider chain from config."""
    cfg = config if config is not None else load_gateway_runtime_config()
    return cfg.get("fallback_providers") or cfg.get("fallback_model") or None


def redact_approval_command(cmd: str | None) -> str:
    """Redact credentials from a command before it enters an approval prompt."""
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(str(cmd or ""), force=True)


def platform_notifications_mode(platform_key: str, *, default: str = "important") -> str:
    """Resolve display.platforms.<platform>.notifications from config."""
    cfg = load_gateway_runtime_config()
    raw = cfg_get(cfg, "display", "platforms", platform_key, "notifications")
    if raw in {None, ""}:
        return default
    return str(raw).strip().lower() or default
