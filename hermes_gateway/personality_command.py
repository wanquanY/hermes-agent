"""Gateway /personality command ownership."""

from __future__ import annotations

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_agent.gateway.runtime_config import load_gateway_runtime_config
from hermes_cli.config import atomic_config_write, cfg_get
from hermes_constants import get_hermes_home

GATEWAY_HOME = get_hermes_home()


def gateway_home():
    return GATEWAY_HOME


def load_gateway_config() -> dict:
    return load_gateway_runtime_config(gateway_home())


def _resolve_prompt(value):
    if isinstance(value, dict):
        parts = [value.get("system_prompt", "")]
        if value.get("tone"):
            parts.append(f'Tone: {value["tone"]}')
        if value.get("style"):
            parts.append(f'Style: {value["style"]}')
        return "\n".join(p for p in parts if p)
    return str(value)


class GatewayPersonalityCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_personality_command(self, event: MessageEvent) -> str:
        """List, set, or clear configured personality overlays."""
        from hermes_constants import display_hermes_home

        args = event.get_command_args().strip().lower()
        config_path = gateway_home() / "config.yaml"

        try:
            config = load_gateway_config()
            personalities = cfg_get(config, "agent", "personalities", default={})
        except Exception:
            config = {}
            personalities = {}

        if not personalities:
            return t("gateway.personality.none_configured", path=display_hermes_home())

        if not args:
            lines = [t("gateway.personality.header")]
            lines.append(t("gateway.personality.none_option"))
            for name, prompt in personalities.items():
                if isinstance(prompt, dict):
                    preview = prompt.get("description") or prompt.get("system_prompt", "")[:50]
                else:
                    preview = prompt[:50] + "..." if len(prompt) > 50 else prompt
                lines.append(t("gateway.personality.item", name=name, preview=preview))
            lines.append(t("gateway.personality.usage"))
            return "\n".join(lines)

        if args in {"none", "default", "neutral"}:
            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = ""
                atomic_config_write(config_path, config)
            except Exception as e:
                return t("gateway.personality.save_failed", error=str(e))
            self._runner._ephemeral_system_prompt = ""
            return t("gateway.personality.cleared")
        if args in personalities:
            new_prompt = _resolve_prompt(personalities[args])

            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = new_prompt
                atomic_config_write(config_path, config)
            except Exception as e:
                return t("gateway.personality.save_failed", error=str(e))

            self._runner._ephemeral_system_prompt = new_prompt
            return t("gateway.personality.set_to", name=args)

        available = "`none`, " + ", ".join(f"`{n}`" for n in personalities)
        return t("gateway.personality.unknown", name=args, available=available)


def personality_command_for(runner) -> GatewayPersonalityCommandService:
    service = getattr(runner, "personality_command", None)
    if isinstance(service, GatewayPersonalityCommandService):
        return service
    service = GatewayPersonalityCommandService(runner)
    runner.personality_command = service
    return service
