"""Telegram processing-reaction lifecycle."""

from __future__ import annotations

import logging

from agent.secret_scope import get_profile_env
from channels.platforms.base import MessageEvent, ProcessingOutcome
from channels.platforms.telegram_security import redact_telegram_error
from channels.platforms.telegram_ids import normalize_telegram_chat_id

logger = logging.getLogger(__name__)


class TelegramReactionsMixin:
    """Own reaction capability and processing-outcome transitions."""

    def _reactions_enabled(self) -> bool:
        configured = self.config.extra.get("reactions")
        raw = (
            str(configured)
            if configured is not None
            else get_profile_env("TELEGRAM_REACTIONS", "false")
        )
        return raw.lower() not in {
            "false",
            "0",
            "no",
        }

    async def _set_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
    ) -> bool:
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=normalize_telegram_chat_id(chat_id),
                message_id=int(message_id),
                reaction=emoji,
            )
            return True
        except Exception as error:
            logger.debug(
                "[%s] set_message_reaction failed (%s): %s",
                self.name,
                emoji,
                redact_telegram_error(error),
            )
            return False

    async def _clear_reactions(self, chat_id: str, message_id: str) -> bool:
        """Clear all bot-set reactions from a Telegram message."""
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=normalize_telegram_chat_id(chat_id),
                message_id=int(message_id),
                reaction=None,
            )
            return True
        except Exception as error:
            logger.debug(
                "[%s] clear reactions failed: %s",
                self.name,
                redact_telegram_error(error),
            )
            return False

    async def on_processing_start(self, event: MessageEvent) -> None:
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id:
            await self._set_reaction(chat_id, message_id, "\U0001f440")

    async def on_processing_complete(
        self,
        event: MessageEvent,
        outcome: ProcessingOutcome,
    ) -> None:
        """Replace the in-progress reaction with the terminal outcome."""
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not (chat_id and message_id):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            await self._clear_reactions(chat_id, message_id)
            return
        emoji = "\U0001f44d" if outcome == ProcessingOutcome.SUCCESS else "\U0001f44e"
        await self._set_reaction(chat_id, message_id, emoji)
