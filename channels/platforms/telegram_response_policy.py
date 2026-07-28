"""Telegram group response-policy extensions."""

from __future__ import annotations

from typing import Any

from agent.secret_scope import get_profile_env

class TelegramResponsePolicyMixin:
    """Topic-scoped opt-outs from mention-only group behavior."""

    def _telegram_free_response_topics(self) -> set[str]:
        raw = self.config.extra.get("free_response_topics")
        if raw is None:
            raw = get_profile_env("TELEGRAM_FREE_RESPONSE_TOPICS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _telegram_is_free_response_topic(self, message: Any) -> bool:
        topics = self._telegram_free_response_topics()
        if not topics:
            return False
        chat_id = str(getattr(getattr(message, "chat", None), "id", ""))
        if not chat_id:
            return False
        thread_id = getattr(message, "message_thread_id", None)
        topic_id = (
            str(thread_id)
            if thread_id is not None
            else self._GENERAL_TOPIC_THREAD_ID
        )
        return f"{chat_id}:{topic_id}" in topics
