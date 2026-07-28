"""Gateway /codex-runtime command ownership."""

from __future__ import annotations

import logging

from channels.platforms.base import MessageEvent
from hermes_gateway.agent_cache import agent_cache_for

logger = logging.getLogger(__name__)


class GatewayCodexRuntimeCommandMixin:
    async def _handle_codex_runtime_command(self, event: MessageEvent) -> str:
        """Toggle codex app-server runtime for the current gateway session."""
        from hermes_cli import codex_runtime_switch as crs

        raw_args = event.get_command_args().strip() if event else ""
        new_value, errors = crs.parse_args(raw_args)
        if errors:
            return "❌ " + "\n❌ ".join(errors)

        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            return f"❌ Could not load config: {exc}"
        cfg = load_config()

        result = crs.apply(
            cfg,
            new_value,
            persist_callback=(save_config if new_value is not None else None),
        )

        if result.success and new_value is not None and result.requires_new_session:
            try:
                session_key = self._session_key_for_source(event.source)
                agent_cache_for(self).evict_cached_agent(session_key)
            except Exception:
                logger.debug(
                    "could not evict cached agent after codex-runtime change",
                    exc_info=True,
                )

        prefix = "✓" if result.success else "✗"
        return f"{prefix} {result.message}"
