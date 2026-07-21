"""Gateway /footer runtime metadata command ownership."""

from __future__ import annotations

import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_agent.gateway.runtime_config import (
    load_gateway_runtime_config,
    resolve_gateway_model as _resolve_gateway_model,
)
from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home
from hermes_gateway.config import Platform
from hermes_gateway.runtime_footer import format_runtime_footer, resolve_footer_config

logger = logging.getLogger(__name__)
GATEWAY_HOME = get_hermes_home()


def gateway_home():
    return GATEWAY_HOME


def load_gateway_config() -> dict:
    return load_gateway_runtime_config(gateway_home())


def resolve_gateway_model(config: dict | None = None) -> str:
    return _resolve_gateway_model(config)


def _platform_config_key(platform: Platform) -> str:
    return "cli" if platform == Platform.LOCAL else platform.value


class GatewayFooterCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_footer_command(self, event: MessageEvent) -> str:
        """Handle /footer command by toggling the global runtime footer flag."""

        config_path = gateway_home() / "config.yaml"
        platform_key = _platform_config_key(event.source.platform)

        try:
            arg = event.get_command_args().strip().lower()
        except Exception:
            arg = ""

        try:
            user_config: dict = load_gateway_config()
        except Exception as e:
            return t("hermes_gateway.config_read_failed", error=e)

        effective = resolve_footer_config(user_config, platform_key)

        if arg in {"status", "?"}:
            state = t("gateway.footer.state_on") if effective["enabled"] else t("gateway.footer.state_off")
            fields = ", ".join(effective.get("fields") or [])
            return t(
                "gateway.footer.status",
                state=state,
                fields=fields,
                platform=platform_key,
            )

        if arg in {"on", "enable", "true", "1"}:
            new_state = True
        elif arg in {"off", "disable", "false", "0"}:
            new_state = False
        elif arg == "":
            new_state = not effective["enabled"]
        else:
            return t("gateway.footer.usage")

        try:
            if not isinstance(user_config.get("display"), dict):
                user_config["display"] = {}
            display = user_config["display"]
            if not isinstance(display.get("runtime_footer"), dict):
                display["runtime_footer"] = {}
            display["runtime_footer"]["enabled"] = new_state
            atomic_config_write(config_path, user_config)
        except Exception as e:
            logger.warning("Failed to save runtime_footer.enabled: %s", e)
            return t("hermes_gateway.config_save_failed", error=e)

        state = t("gateway.footer.state_on") if new_state else t("gateway.footer.state_off")
        example = ""
        if new_state:
            preview = format_runtime_footer(
                model=resolve_gateway_model(user_config) or None,
                context_tokens=0,
                context_length=None,
                fields=effective.get("fields") or ["model", "context_pct", "cwd"],
            )
            if preview:
                example = t("gateway.footer.example_line", preview=preview)
        return t("gateway.footer.saved", state=state, example=example)


def footer_command_for(runner) -> GatewayFooterCommandService:
    service = getattr(runner, "footer_command", None)
    if isinstance(service, GatewayFooterCommandService):
        return service
    service = GatewayFooterCommandService(runner)
    runner.footer_command = service
    return service
