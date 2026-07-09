"""Gateway reasoning slash-command ownership."""

from __future__ import annotations

import logging

from agent.i18n import t
from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get
from channels.platforms.base import MessageEvent
from hermes_gateway.config import Platform
from hermes_gateway.gateway_runtime_config import runtime_config_for
from utils import atomic_yaml_write, is_truthy_value

logger = logging.getLogger(__name__)
GATEWAY_HOME = get_hermes_home()


def gateway_home():
    return GATEWAY_HOME


def _platform_config_key(platform: Platform) -> str:
    return "cli" if platform == Platform.LOCAL else platform.value


class GatewayReasoningCommandMixin:
    @staticmethod
    def _parse_reasoning_command_args(raw_args: str) -> tuple[str, bool]:
        """Parse `/reasoning` args into `(value, persist_global)`.

        `/reasoning <level>` is session-scoped by default. `--global` may be
        supplied in any position to persist the change to config.yaml.
        """
        import shlex

        text = str(raw_args or "").strip().replace("—", "--")
        if not text:
            return "", False
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()

        persist_global = False
        value_tokens = []
        for token in tokens:
            if token == "--global":
                persist_global = True
            else:
                value_tokens.append(token)
        return " ".join(value_tokens).strip().lower(), persist_global

    @staticmethod
    def _load_show_reasoning() -> bool:
        """Load show_reasoning toggle from config.yaml display section."""
        try:
            import yaml as _y
            cfg_path = gateway_home() / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as _f:
                    cfg = _y.safe_load(_f) or {}
                return is_truthy_value(
                    cfg_get(cfg, "display", "show_reasoning"),
                    default=False,
                )
        except Exception:
            pass
        return False

    async def _handle_reasoning_command(self, event: MessageEvent) -> str:
        """Handle /reasoning command — manage reasoning effort and display toggle.

        Usage:
            /reasoning                       Show current effort level and display state
            /reasoning <level>               Set reasoning effort for this session only
            /reasoning <level> --global      Persist reasoning effort to config.yaml
            /reasoning reset                 Clear this session's reasoning override
            /reasoning show|on               Show model reasoning in responses
            /reasoning hide|off              Hide model reasoning from responses
        """
        import yaml

        raw_args = event.get_command_args().strip()
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        config_path = gateway_home() / "config.yaml"
        session_key = self._session_key_for_source(event.source)
        self._show_reasoning = self._load_show_reasoning()
        self._reasoning_config = runtime_config_for(self).resolve_session_reasoning_config(
            source=event.source,
            session_key=session_key,
        )

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

        if not raw_args:
            # Show current state
            rc = self._reasoning_config
            if rc is None:
                level = t("gateway.reasoning.level_default")
            elif rc.get("enabled") is False:
                level = t("gateway.reasoning.level_disabled")
            else:
                level = rc.get("effort", "medium")
            display_state = (
                t("gateway.reasoning.display_on")
                if self._show_reasoning
                else t("gateway.reasoning.display_off")
            )
            has_session_override = session_key in (getattr(self, "_session_reasoning_overrides", {}) or {})
            scope = (
                t("gateway.reasoning.scope_session")
                if has_session_override
                else t("gateway.reasoning.scope_global")
            )
            return t(
                "gateway.reasoning.status",
                level=level,
                scope=scope,
                display=display_state,
            )

        # Display toggle (per-platform)
        platform_key = _platform_config_key(event.source.platform)
        if args in {"show", "on"}:
            self._show_reasoning = True
            _save_config_key(f"display.platforms.{platform_key}.show_reasoning", True)
            return t("gateway.reasoning.display_set_on", platform=platform_key)

        if args in {"hide", "off"}:
            self._show_reasoning = False
            _save_config_key(f"display.platforms.{platform_key}.show_reasoning", False)
            return t("gateway.reasoning.display_set_off", platform=platform_key)

        # Effort level change
        effort = args.strip()
        if effort == "reset":
            if persist_global:
                return t("gateway.reasoning.reset_global_unsupported")
            runtime_config_for(self).set_session_reasoning_override(session_key, None)
            self._reasoning_config = runtime_config_for(self).load_reasoning_config()
            self._evict_cached_agent(session_key)
            return t("gateway.reasoning.reset_done")
        if effort == "none":
            parsed = {"enabled": False}
        elif effort in {"minimal", "low", "medium", "high", "xhigh"}:
            parsed = {"enabled": True, "effort": effort}
        else:
            return t(
                "gateway.reasoning.unknown_arg",
                arg=effort or raw_args.lower(),
            )

        self._reasoning_config = parsed
        if persist_global:
            if _save_config_key("agent.reasoning_effort", effort):
                runtime_config_for(self).set_session_reasoning_override(session_key, None)
                self._evict_cached_agent(session_key)
                return t("gateway.reasoning.set_global", effort=effort)
            runtime_config_for(self).set_session_reasoning_override(session_key, parsed)
            self._evict_cached_agent(session_key)
            return t("gateway.reasoning.set_global_save_failed", effort=effort)

        runtime_config_for(self).set_session_reasoning_override(session_key, parsed)
        self._evict_cached_agent(session_key)
        return t("gateway.reasoning.set_session", effort=effort)
