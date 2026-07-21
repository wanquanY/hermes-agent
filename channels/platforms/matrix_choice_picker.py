"""Matrix reaction-backed finite-choice picker capability."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from agent.secret_scope import get_profile_env
from channels.platforms.base import SendResult
from channels.platforms.matrix_support import _MatrixChoicePickerPrompt

logger = logging.getLogger(__name__)

_REACTIONS = (
    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣",
    "7️⃣", "8️⃣", "9️⃣", "🔟", "🅰️", "🅱️",
)


class MatrixChoicePickerMixin:
    def _choice_picker_store(self) -> dict[str, _MatrixChoicePickerPrompt]:
        store = getattr(self, "_choice_picker_prompts_by_event", None)
        if store is None:
            store = {}
            self._choice_picker_prompts_by_event = store
        return store

    @staticmethod
    def _choice_picker_timeout_seconds() -> int:
        try:
            return max(
                0,
                int(get_profile_env("MATRIX_APPROVAL_TIMEOUT_SECONDS", "300")),
            )
        except ValueError:
            return 300

    async def send_choice_picker(
        self,
        chat_id: str,
        title: str,
        choices: list,
        session_key: str,
        on_choice_selected,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")

        emoji_choices: dict[str, str] = {}
        lines = [title, ""]
        for emoji, choice in zip(_REACTIONS, choices):
            value = str(choice.get("value") or "")
            label = str(choice.get("label") or value)
            if choice.get("is_current"):
                label = f"{label} ← current"
            emoji_choices[emoji] = value
            lines.append(f"{emoji} {label}")
        if not emoji_choices:
            return SendResult(success=False, error="No choices")

        lines.extend(("", "React to choose."))
        result = await self.send(chat_id, "\n".join(lines), metadata=metadata)
        if not result.success or not result.message_id:
            return result

        prompt = _MatrixChoicePickerPrompt(
            chat_id=chat_id,
            message_id=result.message_id,
            session_key=session_key,
            choices=emoji_choices,
            on_choice_selected=on_choice_selected,
            requester_user_id=str((metadata or {}).get("requester_user_id") or "") or None,
            expires_at=time.monotonic() + self._choice_picker_timeout_seconds(),
        )
        self._choice_picker_store()[result.message_id] = prompt
        for emoji in emoji_choices:
            reaction_event_id = await self._send_reaction(
                chat_id,
                result.message_id,
                emoji,
            )
            if reaction_event_id:
                prompt.bot_reaction_events[emoji] = str(reaction_event_id)
        return result

    async def _handle_choice_picker_reaction(
        self,
        *,
        room_id: str,
        reacts_to: str,
        key: str,
        sender: str,
    ) -> bool:
        store = self._choice_picker_store()
        prompt = store.get(reacts_to)
        if prompt is None or prompt.resolved:
            return False
        if room_id != prompt.chat_id:
            return True
        if prompt.expires_at is not None and time.monotonic() > prompt.expires_at:
            store.pop(reacts_to, None)
            return True
        if prompt.requester_user_id and sender != prompt.requester_user_id:
            logger.info(
                "Matrix: ignoring choice picker reaction from non-requester %s",
                sender,
            )
            return True
        if self._allowed_user_ids and sender not in self._allowed_user_ids:
            logger.info(
                "Matrix: ignoring choice picker reaction from unauthorized user %s",
                sender,
            )
            return True

        value = prompt.choices.get(key)
        if value is None:
            return True
        prompt.resolved = True
        store.pop(reacts_to, None)
        try:
            confirmation = await prompt.on_choice_selected(room_id, value)
            for event_id in prompt.bot_reaction_events.values():
                self._schedule_reaction_redaction(
                    room_id,
                    event_id,
                    "choice picker resolved",
                )
            if confirmation:
                await self.send(room_id, confirmation, reply_to=reacts_to)
        except Exception as exc:
            logger.error("Failed to apply choice from Matrix reaction: %s", exc)
            await self.send(
                room_id,
                f"Failed to apply selection: {exc}",
                reply_to=reacts_to,
            )
        return True
