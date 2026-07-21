"""Gateway reasoning slash-command ownership."""

from __future__ import annotations

import logging

from agent.i18n import t
from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get
from channels.platforms.base import MessageEvent
from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.choice_picker import try_send_choice_picker
from hermes_gateway.command_config import save_gateway_config_key
from hermes_gateway.config import Platform
from hermes_gateway.gateway_runtime_config import runtime_config_for
from utils import is_truthy_value

logger = logging.getLogger(__name__)
GATEWAY_HOME = get_hermes_home()


def gateway_home():
    return GATEWAY_HOME


def _platform_config_key(platform: Platform) -> str:
    return "cli" if platform == Platform.LOCAL else platform.value


class GatewayReasoningCommandService:
    def __init__(self, runner):
        self._runner = runner

    @staticmethod
    def parse_reasoning_command_args(raw_args: str) -> tuple[str, bool]:
        """Parse `/reasoning` args into `(value, persist_global)`.

        `/reasoning <level>` is session-scoped by default. `--global` may be
        supplied in any position to persist the change to config.yaml.
        """
        from hermes_cli.session_scope import parse_session_scoped_args

        parsed = parse_session_scoped_args(raw_args)
        return parsed.value.lower(), parsed.persist_global

    @staticmethod
    def load_show_reasoning() -> bool:
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
        except Exception as exc:
            logger.debug("Could not load show_reasoning from gateway config: %s", exc)
        return False

    def _apply_selection(
        self,
        *,
        session_key: str,
        platform_key: str,
        value: str,
        persist_global: bool = False,
    ) -> str:
        """Apply typed and picked values through one canonical state path."""
        from hermes_constants import parse_reasoning_effort

        value = (value or "").strip().lower()
        config_path = gateway_home() / "config.yaml"
        if value in {"show", "on"}:
            self._runner._show_reasoning = True
            save_gateway_config_key(
                config_path,
                f"display.platforms.{platform_key}.show_reasoning",
                True,
            )
            return t("gateway.reasoning.display_set_on", platform=platform_key)
        if value in {"hide", "off"}:
            self._runner._show_reasoning = False
            save_gateway_config_key(
                config_path,
                f"display.platforms.{platform_key}.show_reasoning",
                False,
            )
            return t("gateway.reasoning.display_set_off", platform=platform_key)

        if value == "reset":
            if persist_global:
                return t("gateway.reasoning.reset_global_unsupported")
            runtime_config_for(self._runner).set_session_reasoning_override(
                session_key, None
            )
            self._runner._reasoning_config = runtime_config_for(
                self._runner
            ).load_reasoning_config()
            agent_cache_for(self._runner).evict_cached_agent(session_key)
            return t("gateway.reasoning.reset_done")

        parsed = parse_reasoning_effort(value)
        if parsed is None:
            return t("gateway.reasoning.unknown_arg", arg=value)

        self._runner._reasoning_config = parsed
        runtime = runtime_config_for(self._runner)
        if persist_global:
            if save_gateway_config_key(config_path, "agent.reasoning_effort", value):
                runtime.set_session_reasoning_override(session_key, None)
                agent_cache_for(self._runner).evict_cached_agent(session_key)
                return t("gateway.reasoning.set_global", effort=value)
            runtime.set_session_reasoning_override(session_key, parsed)
            agent_cache_for(self._runner).evict_cached_agent(session_key)
            return t("gateway.reasoning.set_global_save_failed", effort=value)

        runtime.set_session_reasoning_override(session_key, parsed)
        agent_cache_for(self._runner).evict_cached_agent(session_key)
        return t("gateway.reasoning.set_session", effort=value)

    @staticmethod
    def _picker_choices(current_effort: str) -> list[dict[str, object]]:
        from hermes_constants import VALID_REASONING_EFFORTS

        choices: list[dict[str, object]] = [
            {
                "value": "none",
                "label": t("gateway.reasoning.choice_none"),
                "is_current": current_effort == "none",
            }
        ]
        choices.extend(
            {
                "value": level,
                "label": level,
                "is_current": level == current_effort,
            }
            for level in VALID_REASONING_EFFORTS
        )
        choices.extend(
            [
                {"value": "reset", "label": t("gateway.reasoning.choice_reset"), "is_current": False},
                {"value": "show", "label": t("gateway.reasoning.choice_show"), "is_current": False},
                {"value": "hide", "label": t("gateway.reasoning.choice_hide"), "is_current": False},
            ]
        )
        return choices

    async def handle_reasoning_command(self, event: MessageEvent) -> str | None:
        """Handle /reasoning command — manage reasoning effort and display toggle.

        Usage:
            /reasoning                       Show current effort level and display state
            /reasoning <level>               Set reasoning effort for this session only
            /reasoning <level> --global      Persist reasoning effort to config.yaml
            /reasoning reset                 Clear this session's reasoning override
            /reasoning show|on               Show model reasoning in responses
            /reasoning hide|off              Hide model reasoning from responses
        """
        raw_args = event.get_command_args().strip()
        args, persist_global = self.parse_reasoning_command_args(raw_args)
        session_key = self._runner._session_key_for_source(event.source)
        self._runner._show_reasoning = self.load_show_reasoning()
        self._runner._reasoning_config = runtime_config_for(self._runner).resolve_session_reasoning_config(
            source=event.source,
            session_key=session_key,
        )

        if not raw_args:
            # Show current state
            rc = self._runner._reasoning_config
            if rc is None:
                level = t("gateway.reasoning.level_default")
                current_effort = "medium"
            elif rc.get("enabled") is False:
                level = t("gateway.reasoning.level_disabled")
                current_effort = "none"
            else:
                level = rc.get("effort", "medium")
                current_effort = level
            display_state = (
                t("gateway.reasoning.display_on")
                if self._runner._show_reasoning
                else t("gateway.reasoning.display_off")
            )
            has_session_override = session_key in (getattr(self._runner, "_session_reasoning_overrides", {}) or {})
            scope = (
                t("gateway.reasoning.scope_session")
                if has_session_override
                else t("gateway.reasoning.scope_global")
            )
            platform_key = _platform_config_key(event.source.platform)

            async def on_choice(_chat_id: str, value: str) -> str:
                return self._apply_selection(
                    session_key=session_key,
                    platform_key=platform_key,
                    value=value,
                )

            sent = await try_send_choice_picker(
                self._runner,
                event,
                session_key=session_key,
                title=t(
                    "gateway.reasoning.picker_title",
                    level=level,
                    scope=scope,
                    display=display_state,
                ),
                choices=self._picker_choices(current_effort),
                on_choice_selected=on_choice,
            )
            if sent:
                return None
            return t(
                "gateway.reasoning.status",
                level=level,
                scope=scope,
                display=display_state,
            )

        platform_key = _platform_config_key(event.source.platform)
        return self._apply_selection(
            session_key=session_key,
            platform_key=platform_key,
            value=args,
            persist_global=persist_global,
        )


def reasoning_command_for(runner) -> GatewayReasoningCommandService:
    service = getattr(runner, "reasoning_command", None)
    if isinstance(service, GatewayReasoningCommandService):
        return service
    service = GatewayReasoningCommandService(runner)
    runner.reasoning_command = service
    return service
