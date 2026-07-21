"""Cross-surface capability commands for Gateway chat sessions.

These handlers adapt shared domain services to the messaging runtime. They do
not own blueprint filling, skill authoring, model switching, or persistence;
those responsibilities remain in their canonical modules.
"""

from __future__ import annotations

import logging
from typing import Any

from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.model_command import model_command_for

logger = logging.getLogger(__name__)


class GatewayCapabilityCommandService:
    def __init__(self, runner: Any):
        self._runner = runner

    async def _send_ack(self, source: Any, text: str) -> None:
        if not text:
            return
        try:
            adapter = self._runner._adapter_for_source(source)
            if adapter is None:
                return
            metadata = self._runner._thread_metadata_for_source(source)
            await adapter.send(str(source.chat_id), text, metadata=metadata)
        except Exception:
            logger.debug("capability command acknowledgement failed", exc_info=True)

    async def prepare_learn(self, event: Any, source: Any) -> str | None:
        """Rewrite a /learn invocation into the canonical authoring prompt."""
        from agent.learn_prompt import build_learn_prompt

        request = event.get_command_args().strip()
        await self._send_ack(
            source,
            (
                "Learning a skill from what you described…"
                if request
                else "Learning a skill from this conversation…"
            ),
        )
        try:
            event.text = build_learn_prompt(request)
        except Exception:
            logger.exception("could not prepare /learn prompt")
            return "Could not start /learn — please try again."
        return None

    async def prepare_blueprint(self, event: Any, source: Any) -> str | None:
        """Handle or rewrite /blueprint using the shared blueprint service."""
        from hermes_cli.blueprint_cmd import handle_blueprint_command

        origin = None
        try:
            platform = (
                getattr(source.platform, "value", None)
                or str(getattr(source, "platform", "") or "")
            )
            if platform and getattr(source, "chat_id", None):
                origin = {
                    "platform": platform,
                    "chat_id": str(source.chat_id),
                    "chat_name": getattr(source, "chat_name", None),
                    "thread_id": getattr(source, "thread_id", None),
                }
        except Exception:
            logger.debug("could not derive blueprint origin", exc_info=True)

        try:
            result = handle_blueprint_command(
                event.get_command_args().strip(),
                origin=origin,
                surface="gateway",
            )
        except Exception as exc:
            logger.debug("blueprint command failed", exc_info=True)
            return f"Automation blueprint command failed: {exc}"

        if not result.agent_seed:
            return result.text or None
        await self._send_ack(source, result.text)
        event.text = result.agent_seed
        return None

    def prepare_moa(self, event: Any, session_key: str) -> str | None:
        """Lease the default MoA preset for exactly the next agent turn."""
        from hermes_cli.config import load_config
        from hermes_cli.moa_config import moa_usage, normalize_moa_config

        payload = event.get_command_args().strip()
        if not payload:
            return moa_usage()

        config = load_config()
        moa = normalize_moa_config(config.get("moa") or {})
        preset = moa["default_preset"]
        model_service = model_command_for(self._runner)
        snapshot = model_service.snapshot_session_model_override(session_key)

        event.text = payload
        self._runner._session_model_overrides[session_key] = {
            "provider": "moa",
            "model": preset,
            "base_url": "moa://local",
            "api_key": "moa-virtual-provider",
            "api_mode": "chat_completions",
        }
        model_service.stage_one_turn_restore(session_key, snapshot)
        agent_cache_for(self._runner).evict_cached_agent(session_key)
        return None

    @staticmethod
    def version_text() -> str:
        from hermes_cli.banner import format_banner_version_label

        return format_banner_version_label()


def capability_commands_for(runner: Any) -> GatewayCapabilityCommandService:
    service = getattr(runner, "capability_commands", None)
    if isinstance(service, GatewayCapabilityCommandService):
        return service
    service = GatewayCapabilityCommandService(runner)
    runner.capability_commands = service
    return service


__all__ = [
    "GatewayCapabilityCommandService",
    "capability_commands_for",
]
