"""Gateway Priority Processing (/fast) command ownership."""

from __future__ import annotations

import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_agent.gateway.runtime_config import (
    load_gateway_runtime_config,
    resolve_gateway_model as _resolve_gateway_model,
)
from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get
from utils import atomic_yaml_write

logger = logging.getLogger(__name__)
GATEWAY_HOME = get_hermes_home()


def gateway_home():
    return GATEWAY_HOME


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

    async def handle_fast_command(self, event: MessageEvent) -> str:
        """Handle /fast — mirror the CLI Priority Processing toggle in gateway chats."""
        import yaml
        from hermes_cli.models import model_supports_fast_mode

        args = event.get_command_args().strip().lower()
        config_path = gateway_home() / "config.yaml"
        self._runner._service_tier = self.load_service_tier()

        user_config = load_gateway_config()
        model = resolve_gateway_model(user_config)
        if not model_supports_fast_mode(model):
            return t("gateway.fast.not_supported")

        def _save_config_key(key_path: str, value):
            """Save a dot-separated key to config.yaml."""
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                keys = key_path.split(".")
                current = user_config
                for k in keys[:-1]:
                    if k not in current or not isinstance(current[k], dict):
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value
                atomic_yaml_write(config_path, user_config)
                return True
            except Exception as e:
                logger.error("Failed to save config key %s: %s", key_path, e)
                return False

        if not args or args == "status":
            status = t("gateway.fast.status_fast") if self._runner._service_tier == "priority" else t("gateway.fast.status_normal")
            return t("gateway.fast.status", mode=status)

        if args in {"fast", "on"}:
            self._runner._service_tier = "priority"
            saved_value = "fast"
            label = t("gateway.fast.label_fast")
        elif args in {"normal", "off"}:
            self._runner._service_tier = None
            saved_value = "normal"
            label = t("gateway.fast.label_normal")
        else:
            return t("gateway.fast.unknown_arg", arg=args)

        if _save_config_key("agent.service_tier", saved_value):
            return t("gateway.fast.saved", label=label)
        return t("gateway.fast.session_only", label=label)


def fast_command_for(runner) -> GatewayFastCommandService:
    service = getattr(runner, "fast_command", None)
    if isinstance(service, GatewayFastCommandService):
        return service
    service = GatewayFastCommandService(runner)
    runner.fast_command = service
    return service
