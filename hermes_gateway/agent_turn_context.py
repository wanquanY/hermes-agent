"""Byte-stable Gateway context and volatile per-turn note ownership."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_gateway.config import Platform
from hermes_gateway.session_context import (
    _discord_tools_loaded,
    build_session_context_prompt,
)


class GatewayAgentTurnContextService:
    """Separates stable system context from current-turn wire context."""

    def __init__(self, runner):
        self._runner = runner

    def pinned_context_prompt(
        self,
        *,
        context: Any,
        redact_pii: bool,
        session_key: str,
    ) -> str:
        """Reuse rendered system-prompt bytes until a real input changes."""
        pins = getattr(self._runner, "_session_context_prompt_pins", None)
        if not isinstance(pins, dict):
            pins = {}
            self._runner._session_context_prompt_pins = pins
        change_key = self._context_change_key(context, redact_pii)
        pinned = pins.get(session_key) if session_key else None
        if isinstance(pinned, tuple) and len(pinned) == 2 and pinned[0] == change_key:
            return str(pinned[1])
        rendered = build_session_context_prompt(context, redact_pii=redact_pii)
        if session_key:
            pins[session_key] = (change_key, rendered)
        return rendered

    async def collect_turn_notes(
        self,
        *,
        history: list[dict[str, Any]],
        source: Any,
        event: Any,
        session_key: str,
        home_target_env_var,
        platform_notice_for,
        voice_runtime_for,
    ) -> list[str]:
        """Return volatile facts that must ride only this user message."""
        notes: list[str] = []
        runner = self._runner
        if not history and not await run_sqlite_io(
            runner.session_store.has_any_sessions
        ):
            notes.append(
                "[System note: This is the user's very first message ever. "
                "Briefly introduce yourself and mention that /help shows available "
                "commands. Keep the introduction concise -- one or two sentences max.]"
            )

        if (
            not history
            and source.platform
            and source.platform not in {Platform.LOCAL, Platform.WEBHOOK}
        ):
            platform_name = source.platform.value
            env_key = home_target_env_var(platform_name)
            if not os.getenv(env_key):
                sethome_cmd = (
                    "/hermes sethome"
                    if source.platform == Platform.SLACK
                    else "/sethome"
                )
                notice = (
                    f"📬 No home channel is set for {platform_name.title()}. "
                    "A home channel is where Hermes delivers cron job results "
                    "and cross-platform messages.\n\n"
                    f"Type {sethome_cmd} to make this chat your home channel, "
                    "or ignore to skip."
                )
                await platform_notice_for(runner).deliver_platform_notice(
                    source, notice
                )

        voice_note = self._voice_channel_note(
            event=event,
            source=source,
            session_key=session_key,
            voice_runtime_for=voice_runtime_for,
        )
        if voice_note:
            notes.append(voice_note)

        if (
            source.platform == Platform.DISCORD
            and getattr(event, "message_id", None)
            and _discord_tools_loaded()
        ):
            notes.append(
                f"[Triggering message id: `{event.message_id}` — use as `message_id` "
                "for reply/react/pin via the discord tools.]"
            )
        return notes

    def _voice_channel_note(
        self,
        *,
        event: Any,
        source: Any,
        session_key: str,
        voice_runtime_for,
    ) -> str:
        if source.platform != Platform.DISCORD:
            return ""
        adapter = self._runner.adapters.get(Platform.DISCORD)
        guild_id = voice_runtime_for(self._runner).get_guild_id(event)
        if not (guild_id and adapter and hasattr(adapter, "get_voice_channel_context")):
            return ""
        try:
            current = str(adapter.get_voice_channel_context(guild_id) or "")
        except Exception:
            return ""
        previous_by_session = getattr(self._runner, "_session_voice_context", None)
        if not isinstance(previous_by_session, dict):
            previous_by_session = {}
            self._runner._session_voice_context = previous_by_session
        previous = previous_by_session.get(session_key)
        if session_key:
            previous_by_session[session_key] = current
        if current == (previous if previous is not None else ""):
            return ""
        if not current:
            return "[Voice channel now: not connected to a voice channel]"
        return f"[Voice channel now: {current}]"

    @staticmethod
    def _context_change_key(context: Any, redact_pii: bool) -> str:
        """Hash every input rendered into the stable session prompt."""
        source = context.source
        platform = source.platform.value if source.platform else ""
        discord_ids: tuple[str, ...] = ()
        discord_tools = ""
        if source.platform == Platform.DISCORD:
            discord_tools = "1" if _discord_tools_loaded() else "0"
            discord_ids = (
                str(source.guild_id or ""),
                str(source.parent_chat_id or ""),
                str(source.thread_id or ""),
                str(source.chat_id or ""),
                "1" if source.message_id else "0",
            )
        try:
            from hermes_constants import display_hermes_home

            home_display = str(display_hermes_home())
        except Exception:
            home_display = ""
        key = (
            platform,
            str(source.chat_id or ""),
            str(source.thread_id or ""),
            str(source.chat_type or ""),
            str(source.chat_name or ""),
            str(source.chat_topic or ""),
            str(source.user_name or ""),
            str(source.user_id or ""),
            str(getattr(source, "profile", None) or ""),
            bool(context.shared_multi_user_session),
            discord_ids,
            discord_tools,
            tuple(platform.value for platform in context.connected_platforms),
            tuple(
                (
                    platform.value,
                    str(getattr(home, "name", "") or ""),
                    str(getattr(home, "chat_id", "") or ""),
                )
                for platform, home in context.home_channels.items()
            ),
            bool(redact_pii),
            home_display,
        )
        return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()


def agent_turn_context_for(runner) -> GatewayAgentTurnContextService:
    service = getattr(runner, "agent_turn_context", None)
    if isinstance(service, GatewayAgentTurnContextService):
        return service
    service = GatewayAgentTurnContextService(runner)
    runner.agent_turn_context = service
    return service
