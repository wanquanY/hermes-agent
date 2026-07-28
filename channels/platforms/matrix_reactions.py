"""Matrix reaction and approval lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from channels.platforms.base import MessageEvent, ProcessingOutcome
from channels.platforms.matrix_support import EventType, RoomID, _MatrixApprovalPrompt

logger = logging.getLogger(__name__)


class MatrixReactionMixin:
    async def _send_reaction(
        self,
        room_id: str,
        event_id: str,
        emoji: str,
    ) -> Optional[str]:
        """Send an emoji reaction to a message in a room.
        Returns the reaction event_id on success, None on failure.
        """

        if not self._client:
            return None
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": emoji,
            }
        }
        try:
            resp_event_id = await self._client.send_message_event(
                RoomID(room_id),
                EventType.REACTION,
                content,
            )
            logger.debug("Matrix: sent reaction %s to %s", emoji, event_id)
            return str(resp_event_id)
        except Exception as exc:
            logger.debug("Matrix: reaction send error: %s", exc)
            return None

    async def _redact_reaction(
        self,
        room_id: str,
        reaction_event_id: str,
        reason: str = "",
    ) -> bool:
        """Remove a reaction by redacting its event."""
        return await self.redact_message(room_id, reaction_event_id, reason)

    def _schedule_reaction_redaction(
        self,
        room_id: str,
        reaction_event_id: str,
        reason: str = "",
    ) -> None:
        """Redact a reaction after a short delay so message delivery settles."""

        async def _redact_later() -> None:
            try:
                if self._reaction_redaction_delay_seconds:
                    await asyncio.sleep(self._reaction_redaction_delay_seconds)
                if not await self._redact_reaction(room_id, reaction_event_id, reason):
                    logger.debug(
                        "Matrix: failed to redact reaction %s", reaction_event_id
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "Matrix: delayed reaction redaction failed for %s: %s",
                    reaction_event_id,
                    exc,
                )

        task = asyncio.create_task(_redact_later())
        self._reaction_redaction_tasks.add(task)
        task.add_done_callback(self._reaction_redaction_tasks.discard)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add eyes reaction when the agent starts processing a message."""
        if not self._reactions_enabled:
            return
        msg_id = event.message_id
        room_id = event.source.chat_id
        if msg_id and room_id:
            reaction_event_id = await self._send_reaction(room_id, msg_id, "\U0001f440")
            if reaction_event_id:
                self._pending_reactions[(room_id, msg_id)] = reaction_event_id

    async def on_processing_complete(
        self,
        event: MessageEvent,
        outcome: ProcessingOutcome,
    ) -> None:
        """Replace eyes with checkmark (success) or cross (failure)."""
        if not self._reactions_enabled:
            return
        msg_id = event.message_id
        room_id = event.source.chat_id
        if not msg_id or not room_id:
            return
        if outcome == ProcessingOutcome.CANCELLED:
            return
        reaction_key = (room_id, msg_id)
        if reaction_key in self._pending_reactions:
            eyes_event_id = self._pending_reactions.pop(reaction_key)
            self._schedule_reaction_redaction(
                room_id,
                eyes_event_id,
                "processing complete",
            )
        await self._send_reaction(
            room_id,
            msg_id,
            "\u2705" if outcome == ProcessingOutcome.SUCCESS else "\u274c",
        )

    async def _on_reaction(self, event: Any) -> None:
        """Handle incoming reaction events."""
        sender = str(getattr(event, "sender", ""))
        if self._is_self_sender(sender):
            return
        event_id = str(getattr(event, "event_id", ""))
        if self._is_duplicate_event(event_id):
            return

        room_id = str(getattr(event, "room_id", ""))
        content = getattr(event, "content", None)
        if content:
            relates_to = (
                content.get("m.relates_to", {})
                if isinstance(content, dict)
                else getattr(content, "relates_to", {})
            )
            reacts_to = ""
            key = ""
            if isinstance(relates_to, dict):
                reacts_to = relates_to.get("event_id", "")
                key = relates_to.get("key", "")
            elif hasattr(relates_to, "event_id"):
                reacts_to = str(getattr(relates_to, "event_id", ""))
                key = str(getattr(relates_to, "key", ""))
            logger.info(
                "Matrix: reaction %s from %s on %s in %s",
                key,
                sender,
                reacts_to,
                room_id,
            )

            if await self._handle_choice_picker_reaction(
                room_id=room_id,
                reacts_to=reacts_to,
                key=key,
                sender=sender,
            ):
                return

            # Check if this reaction resolves a pending approval prompt.
            prompt = self._approval_prompts_by_event.get(reacts_to)
            if prompt and not prompt.resolved:
                if room_id != prompt.chat_id:
                    return
                if self._allowed_user_ids and sender not in self._allowed_user_ids:
                    logger.info(
                        "Matrix: ignoring approval reaction from unauthorized user %s on %s",
                        sender, reacts_to,
                    )
                    return
                choice = self._approval_reaction_map.get(key)
                if not choice:
                    return
                try:
                    from tools.approval import resolve_gateway_approval

                    count = resolve_gateway_approval(prompt.session_key, choice)
                    if count:
                        prompt.resolved = True
                        self._approval_prompts_by_event.pop(reacts_to, None)
                        self._approval_prompt_by_session.pop(prompt.session_key, None)
                        logger.info(
                            "Matrix reaction resolved %d approval(s) for session %s "
                            "(choice=%s, user=%s)",
                            count, prompt.session_key, choice, sender,
                        )
                        # Redact bot's seed reactions, leaving only the user's
                        await self._redact_bot_approval_reactions(room_id, prompt)
                except Exception as exc:
                    logger.error("Failed to resolve gateway approval from Matrix reaction: %s", exc)

    async def _redact_bot_approval_reactions(
        self,
        room_id: str,
        prompt: "_MatrixApprovalPrompt",
    ) -> None:
        """Redact the bot's seed ✅/❎ reactions, leaving only the user's reaction."""
        for emoji, evt_id in prompt.bot_reaction_events.items():
            self._schedule_reaction_redaction(room_id, evt_id, "approval resolved")
            logger.debug("Matrix: scheduled bot reaction redaction %s (%s)", emoji, evt_id)

    # ------------------------------------------------------------------
    # Text message aggregation (handles Matrix client-side splits)
    # ------------------------------------------------------------------
