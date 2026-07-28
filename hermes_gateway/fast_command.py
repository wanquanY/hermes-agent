"""Gateway Priority Processing (/fast) command ownership."""

from __future__ import annotations

import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_agent.gateway.runtime_config import (
    load_gateway_runtime_config,
    resolve_gateway_model as _resolve_gateway_model,
)
from hermes_constants import get_hermes_home, get_hermes_home_override
from hermes_cli.config import cfg_get
from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.choice_picker import try_send_choice_picker
from hermes_gateway.command_config import save_gateway_config_key

logger = logging.getLogger(__name__)
GATEWAY_HOME = get_hermes_home()


def gateway_home():
    return get_hermes_home_override() or GATEWAY_HOME


def load_gateway_config() -> dict:
    return load_gateway_runtime_config(gateway_home())


def resolve_gateway_model(config: dict | None = None) -> str:
    return _resolve_gateway_model(config)


class GatewayFastCommandService:
    def __init__(self, runner):
        self._runner = runner

    @staticmethod
    def load_service_tier() -> str | None:
        """Load Priority Processing setting from config.yaml.

        Reads agent.service_tier from config.yaml. Accepted values mirror the CLI:
        "fast"/"priority"/"on" => "priority", while "normal"/"off" disables it.
        Returns None when unset or unsupported.
        """
        raw = ""
        try:
            import yaml as _y
            cfg_path = gateway_home() / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                raw = str(cfg_get(cfg, "agent", "service_tier", default="") or "").strip()
        except Exception as exc:
            logger.debug("Could not load service_tier from gateway config: %s", exc)

        value = raw.lower()
        if not value or value in {"normal", "default", "standard", "off", "none"}:
            return None
        if value in {"fast", "priority", "on"}:
            return "priority"
        logger.warning("Unknown service_tier '%s', ignoring", raw)
        return None

    def resolve_session_service_tier(
        self,
        *,
        source=None,
        session_key: str = "",
    ) -> str | None:
        """Resolve a session override before the durable config default."""
        if not session_key and source is not None:
            session_key = self._runner._session_key_for_source(source)
        overrides = getattr(self._runner, "_session_service_tier_overrides", {}) or {}
        if session_key and session_key in overrides:
            return overrides[session_key]
        return self.load_service_tier()

    def set_session_service_tier_override(
        self,
        session_key: str,
        service_tier: str | None,
        *,
        clear: bool = False,
    ) -> None:
        """Set an explicit fast/normal override without cross-session leakage."""
        if not session_key:
            return
        overrides = self._runner.__dict__.get("_session_service_tier_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
            self._runner._session_service_tier_overrides = overrides
        if clear:
            overrides.pop(session_key, None)
        else:
            # Key presence distinguishes explicit normal (None) from no override.
            overrides[session_key] = service_tier

    def _apply_selection(
        self,
        value: str,
        *,
        session_key: str,
        persist_global: bool,
    ) -> str:
        value = (value or "").strip().lower()
        if value in {"fast", "on"}:
            service_tier = "priority"
            saved_value = "fast"
            label = t("gateway.fast.label_fast")
        elif value in {"normal", "off"}:
            service_tier = None
            saved_value = "normal"
            label = t("gateway.fast.label_normal")
        else:
            return t("gateway.fast.unknown_arg", arg=value)

        self._runner._service_tier = service_tier
        if persist_global:
            if save_gateway_config_key(
                gateway_home() / "config.yaml",
                "agent.service_tier",
                saved_value,
            ):
                self.set_session_service_tier_override(
                    session_key,
                    None,
                    clear=True,
                )
                agent_cache_for(self._runner).evict_cached_agent(session_key)
                return t("gateway.fast.saved", label=label)
            self.set_session_service_tier_override(session_key, service_tier)
            agent_cache_for(self._runner).evict_cached_agent(session_key)
            return t("gateway.fast.session_only", label=label)
        self.set_session_service_tier_override(session_key, service_tier)
        agent_cache_for(self._runner).evict_cached_agent(session_key)
        return t("gateway.fast.session_only", label=label)

    async def handle_fast_command(self, event: MessageEvent) -> str | None:
        """Handle session-scoped /fast; ``--global`` persists explicitly."""
        from hermes_cli.models import model_supports_fast_mode
        from hermes_cli.session_scope import parse_session_scoped_args

        scoped = parse_session_scoped_args(event.get_command_args())
        args = scoped.value.lower()
        session_key = self._runner._session_key_for_source(event.source)
        self._runner._service_tier = self.resolve_session_service_tier(
            session_key=session_key
        )

        user_config = load_gateway_config()
        model_override = (
            (getattr(self._runner, "_session_model_overrides", {}) or {}).get(
                session_key,
                {},
            )
        )
        model = str(model_override.get("model") or resolve_gateway_model(user_config))
        if not model_supports_fast_mode(model):
            return t("gateway.fast.not_supported")

        if not args or args == "status":
            is_fast = self._runner._service_tier == "priority"
            status = t("gateway.fast.status_fast") if is_fast else t("gateway.fast.status_normal")

            async def on_choice(_chat_id: str, value: str) -> str:
                return self._apply_selection(
                    value,
                    session_key=session_key,
                    persist_global=scoped.persist_global,
                )

            sent = await try_send_choice_picker(
                self._runner,
                event,
                session_key=session_key,
                title=t("gateway.fast.picker_title", mode=status),
                choices=[
                    {
                        "value": "fast",
                        "label": t("gateway.fast.choice_fast"),
                        "is_current": is_fast,
                    },
                    {
                        "value": "normal",
                        "label": t("gateway.fast.choice_normal"),
                        "is_current": not is_fast,
                    },
                ],
                on_choice_selected=on_choice,
            )
            if sent:
                return None
            return t("gateway.fast.status", mode=status)
        return self._apply_selection(
            args,
            session_key=session_key,
            persist_global=scoped.persist_global,
        )


def fast_command_for(runner) -> GatewayFastCommandService:
    service = getattr(runner, "fast_command", None)
    if isinstance(service, GatewayFastCommandService):
        return service
    service = GatewayFastCommandService(runner)
    runner.fast_command = service
    return service
