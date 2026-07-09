"""Gateway runner runtime configuration owner."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_agent.gateway.runtime_config import (
    load_fallback_model,
    load_gateway_runtime_config,
    load_reasoning_config,
    resolve_gateway_model,
    resolve_runtime_agent_kwargs,
)
from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_home
from hermes_gateway.model_command import model_command_for
from hermes_gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    parse_restart_drain_timeout,
)

logger = logging.getLogger(__name__)
_hermes_home = get_hermes_home()


def _read_gateway_config() -> dict:
    return load_gateway_runtime_config(_hermes_home)


class GatewayRuntimeConfigService:

    def __init__(self, runner):
        self._runner = runner

    def resolve_session_agent_runtime(
        self,
        *,
        source=None,
        session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
    ) -> tuple[str, dict]:
        """Resolve model/runtime for a session, honoring session-scoped /model overrides."""
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._runner._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        model = resolve_gateway_model(user_config)
        override = self._runner._session_model_overrides.get(resolved_session_key) if resolved_session_key else None
        if override:
            override_model = override.get("model", model)
            override_runtime = {
                "provider": override.get("provider"),
                "api_key": override.get("api_key"),
                "base_url": override.get("base_url"),
                "api_mode": override.get("api_mode"),
            }
            if override_runtime.get("api_key"):
                logger.debug(
                    "Session model override (fast): session=%s config_model=%s -> override_model=%s provider=%s",
                    resolved_session_key or "", model, override_model,
                    override_runtime.get("provider"),
                )
                return override_model, override_runtime
            logger.debug(
                "Session model override (no api_key, fallback): session=%s config_model=%s override_model=%s",
                resolved_session_key or "", model, override_model,
            )
        else:
            logger.debug(
                "No session model override: session=%s config_model=%s override_keys=%s",
                resolved_session_key or "", model,
                list(self._runner._session_model_overrides.keys())[:5] if self._runner._session_model_overrides else "[]",
            )

        runtime_kwargs = resolve_runtime_agent_kwargs(_hermes_home)
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            logger.info(
                "Runtime provider supplied explicit model override: %s -> %s",
                model,
                runtime_model,
            )
            model = runtime_model
        if override and resolved_session_key:
            model, runtime_kwargs = model_command_for(self._runner).apply_session_model_override(
                resolved_session_key, model, runtime_kwargs
            )

        if not model and runtime_kwargs.get("provider"):
            try:
                from hermes_cli.models import get_default_model_for_provider

                model = get_default_model_for_provider(runtime_kwargs["provider"])
                if model:
                    logger.info(
                        "No model configured — defaulting to %s for provider %s",
                        model, runtime_kwargs["provider"],
                    )
            except Exception:
                pass

        return model, runtime_kwargs

    def resolve_turn_agent_config(self, user_message: str, model: str, runtime_kwargs: dict) -> dict:
        """Build the effective model/runtime config for a single turn."""
        from hermes_cli.models import resolve_fast_mode_overrides

        runtime = {
            "api_key": runtime_kwargs.get("api_key"),
            "base_url": runtime_kwargs.get("base_url"),
            "provider": runtime_kwargs.get("provider"),
            "api_mode": runtime_kwargs.get("api_mode"),
            "command": runtime_kwargs.get("command"),
            "args": list(runtime_kwargs.get("args") or []),
            "credential_pool": runtime_kwargs.get("credential_pool"),
        }
        route = {
            "model": model,
            "runtime": runtime,
            "signature": (
                model,
                runtime["provider"],
                runtime["base_url"],
                runtime["api_mode"],
                runtime["command"],
                tuple(runtime["args"]),
            ),
        }

        service_tier = getattr(self._runner, "_service_tier", None)
        if not service_tier:
            route["request_overrides"] = {}
            return route

        try:
            overrides = resolve_fast_mode_overrides(route["model"])
        except Exception:
            overrides = None
        route["request_overrides"] = overrides or {}
        return route

    @staticmethod
    def load_prefill_messages() -> list[Dict[str, Any]]:
        """Load ephemeral prefill messages from config or env var."""
        file_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
        if not file_path:
            file_path = _read_gateway_config().get("prefill_messages_file", "")
        if not file_path:
            return []
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = _hermes_home / path
        if not path.exists():
            logger.warning("Prefill messages file not found: %s", path)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("Prefill messages file must contain a JSON array: %s", path)
                return []
            return data
        except Exception as exc:
            logger.warning("Failed to load prefill messages from %s: %s", path, exc)
            return []

    @staticmethod
    def load_ephemeral_system_prompt() -> str:
        """Load ephemeral system prompt from config or env var."""
        prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
        if prompt:
            return prompt
        return (cfg_get(_read_gateway_config(), "agent", "system_prompt", default="") or "").strip()

    @staticmethod
    def load_reasoning_config() -> dict | None:
        return load_reasoning_config(_read_gateway_config())

    def resolve_session_reasoning_config(
        self,
        *,
        source=None,
        session_key: Optional[str] = None,
    ) -> dict | None:
        """Resolve reasoning effort for a session, honoring session overrides."""
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._runner._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        overrides = getattr(self._runner, "_session_reasoning_overrides", {}) or {}
        if resolved_session_key and resolved_session_key in overrides:
            return overrides[resolved_session_key]
        return self.load_reasoning_config()

    def set_session_reasoning_override(
        self,
        session_key: str,
        reasoning_config: Optional[dict],
    ) -> None:
        """Set or clear the session-scoped reasoning override."""
        if not session_key:
            return
        if not hasattr(self._runner, "_session_reasoning_overrides"):
            self._runner._session_reasoning_overrides = {}
        if reasoning_config is None:
            self._runner._session_reasoning_overrides.pop(session_key, None)
        else:
            self._runner._session_reasoning_overrides[session_key] = dict(reasoning_config)

    @staticmethod
    def load_busy_input_mode() -> str:
        """Load gateway drain-time busy-input behavior from config/env."""
        mode = os.getenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "").strip().lower()
        if not mode:
            mode = str(cfg_get(_read_gateway_config(), "display", "busy_input_mode", default="") or "").strip().lower()
        if mode == "queue":
            return "queue"
        if mode == "steer":
            return "steer"
        return "interrupt"

    @staticmethod
    def load_restart_drain_timeout() -> float:
        """Load graceful gateway restart/stop drain timeout in seconds."""
        raw = os.getenv("HERMES_RESTART_DRAIN_TIMEOUT", "").strip()
        if not raw:
            raw = str(cfg_get(_read_gateway_config(), "agent", "restart_drain_timeout", default="") or "").strip()
        value = parse_restart_drain_timeout(raw)
        if raw and value == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT:
            try:
                float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid restart_drain_timeout '%s', using default %.0fs",
                    raw,
                    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
                )
        return value

    @staticmethod
    def load_background_notifications_mode() -> str:
        """Load background process notification mode from config or env var."""
        mode = os.getenv("HERMES_BACKGROUND_NOTIFICATIONS", "")
        if not mode:
            raw = cfg_get(_read_gateway_config(), "display", "background_process_notifications")
            if raw is False:
                mode = "off"
            elif raw not in {None, ""}:
                mode = str(raw)
        mode = (mode or "all").strip().lower()
        valid = {"all", "result", "error", "off"}
        if mode not in valid:
            logger.warning(
                "Unknown background_process_notifications '%s', defaulting to 'all'",
                mode,
            )
            return "all"
        return mode

    @staticmethod
    def load_provider_routing() -> dict:
        """Load OpenRouter provider routing preferences from config.yaml."""
        cfg = _read_gateway_config()
        return cfg.get("provider_routing", {}) or {}

    @staticmethod
    def load_fallback_model() -> list | dict | None:
        return load_fallback_model(_read_gateway_config())


def runtime_config_for(runner) -> GatewayRuntimeConfigService:
    service = getattr(runner, "runtime_config", None)
    if isinstance(service, GatewayRuntimeConfigService):
        return service
    service = GatewayRuntimeConfigService(runner)
    runner.runtime_config = service
    return service
