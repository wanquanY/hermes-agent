"""Shared admission policy for live and recovered Discord messages."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from agent.secret_scope import get_profile_env
try:
    import discord
except ImportError:  # pragma: no cover - optional dependency
    discord = None


class DiscordIngressMixin:
    """Apply one authorization and bot-addressing policy to every ingress."""

    @staticmethod
    def _raw_mentioned_user_ids(message: Any) -> set[str]:
        content = getattr(message, "content", "") or ""
        return {
            match.group(1)
            for match in re.finditer(r"<@!?(\d+)>", content)
        }

    def _self_is_explicitly_mentioned(self, message: Any) -> bool:
        if not self._client or not self._client.user:
            return False
        if self._client.user in getattr(message, "mentions", []):
            return True
        return str(self._client.user.id) in self._raw_mentioned_user_ids(message)

    def _self_is_raw_mentioned(self, message: Any) -> bool:
        if not self._client or not self._client.user:
            return False
        return str(self._client.user.id) in self._raw_mentioned_user_ids(message)

    def _discord_bots_require_inline_mention(self) -> bool:
        configured = self.config.extra.get("bots_require_inline_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return get_profile_env(
            "DISCORD_BOTS_REQUIRE_INLINE_MENTION",
            "false",
        ).lower() in {"true", "1", "yes", "on"}

    def _discord_channel_keys(
        self,
        message: Any,
        parent_channel_id: Optional[str] = None,
    ) -> set[str]:
        return self._discord_channel_keys_from_channel(
            getattr(message, "channel", None),
            parent_channel_id,
        )

    def _discord_channel_keys_from_channel(
        self,
        channel: Any,
        parent_channel_id: Optional[str] = None,
    ) -> set[str]:
        keys: set[str] = set()
        channel_id = getattr(channel, "id", None)
        if channel_id is not None:
            keys.add(str(channel_id))
        channel_name = str(getattr(channel, "name", "")).strip()
        if channel_name:
            keys.update({channel_name, f"#{channel_name}"})
        parent = getattr(channel, "parent", None)
        parent_id = parent_channel_id or getattr(channel, "parent_id", None)
        if parent_id:
            keys.add(str(parent_id))
        parent_name = str(getattr(parent, "name", "")).strip()
        if parent_name:
            keys.update({parent_name, f"#{parent_name}"})
        return keys

    def _discord_message_admission(
        self,
        message: Any,
        *,
        claim: bool,
    ) -> tuple[bool, bool]:
        message_id = str(getattr(message, "id", ""))
        if claim:
            if self._dedup.is_duplicate(message_id):
                return False, False
        elif self._dedup.contains(message_id):
            return False, False
        if message.author == self._client.user:
            return False, False
        if message.type not in {
            discord.MessageType.default,
            discord.MessageType.reply,
        }:
            return False, False

        role_authorized = False
        if getattr(message.author, "bot", False):
            allow_bots = get_profile_env(
                "DISCORD_ALLOW_BOTS", "none"
            ).lower().strip()
            if allow_bots == "none":
                return False, False
            if allow_bots == "mentions" and not self._self_is_explicitly_mentioned(
                message
            ):
                return False, False
            if (
                self._discord_bots_require_inline_mention()
                and not self._self_is_raw_mentioned(message)
            ):
                return False, False
        else:
            guild = getattr(message, "guild", None)
            is_dm = isinstance(message.channel, discord.DMChannel) or guild is None
            channel_ids = None
            if not is_dm:
                channel_ids = self._discord_channel_keys(message)
            if not self._is_allowed_user(
                str(message.author.id),
                message.author,
                guild=guild,
                is_dm=is_dm,
                channel_ids=channel_ids,
            ):
                self._warn_if_fail_closed_default()
                return False, False
            role_authorized = bool(getattr(self, "_allowed_role_ids", set()))

        raw_self_mention = self._self_is_explicitly_mentioned(message)
        if not isinstance(message.channel, discord.DMChannel) and (
            getattr(message, "mentions", None) or raw_self_mention
        ):
            other_bots_mentioned = any(
                mentioned.bot and mentioned != self._client.user
                for mentioned in getattr(message, "mentions", [])
            )
            if other_bots_mentioned and not raw_self_mention:
                return False, False
            ignore_no_mention = get_profile_env(
                "DISCORD_IGNORE_NO_MENTION",
                "true",
            ).lower() in {"true", "1", "yes"}
            if ignore_no_mention and not raw_self_mention and not other_bots_mentioned:
                free_channels = self._discord_free_response_channels()
                channel_keys = self._discord_channel_keys(message)
                if "*" not in free_channels and not (channel_keys & free_channels):
                    return False, False
        return True, role_authorized

    async def _dispatch_discord_message(self, message: Any) -> bool:
        if not self._ready_event.is_set():
            try:
                await asyncio.wait_for(self._ready_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
        admitted, role_authorized = self._discord_message_admission(
            message,
            claim=True,
        )
        if not admitted:
            return False
        return await self._handle_message(
            message,
            role_authorized=role_authorized,
        )
