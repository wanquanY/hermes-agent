"""Gateway conversation editing commands: /retry, /undo, /suggestions."""

from __future__ import annotations

import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent, MessageType
from hermes_agent.composition.async_sqlite import run_sqlite_io

logger = logging.getLogger(__name__)


class GatewayConversationEditingCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_retry_command(self, event: MessageEvent) -> str:
        """Replay the last user message after removing its old response."""

        source = event.source
        session_entry = await run_sqlite_io(
            self._runner.session_store.get_or_create_session,
            source,
        )
        history = await run_sqlite_io(
            self._runner.session_store.load_transcript,
            session_entry.session_id,
        )

        last_user_msg = None
        last_user_idx = None
        for idx in range(len(history) - 1, -1, -1):
            if history[idx].get("role") == "user":
                last_user_msg = history[idx].get("content", "")
                last_user_idx = idx
                break

        if not last_user_msg:
            return t("gateway.retry.no_previous")

        stability = getattr(
            getattr(self._runner, "_session_db", None),
            "runtime_stability",
            None,
        )
        if stability is not None:
            await run_sqlite_io(
                stability.clear_stream_stale,
                session_entry.session_id,
            )

        await run_sqlite_io(
            self._runner.session_store.rewrite_transcript,
            session_entry.session_id,
            history[:last_user_idx],
        )
        session_entry.last_prompt_tokens = 0

        retry_event = MessageEvent(
            text=last_user_msg,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=event.raw_message,
            channel_prompt=event.channel_prompt,
        )
        return await self._runner._handle_message(retry_event)

    async def handle_undo_command(self, event: MessageEvent) -> str:
        """Remove the last user/assistant exchange from the transcript."""

        source = event.source
        session_entry = await run_sqlite_io(
            self._runner.session_store.get_or_create_session,
            source,
        )
        history = await run_sqlite_io(
            self._runner.session_store.load_transcript,
            session_entry.session_id,
        )

        last_user_idx = None
        for idx in range(len(history) - 1, -1, -1):
            if history[idx].get("role") == "user":
                last_user_idx = idx
                break

        if last_user_idx is None:
            return t("gateway.undo.nothing")

        removed_msg = history[last_user_idx].get("content", "")
        removed_count = len(history) - last_user_idx
        await run_sqlite_io(
            self._runner.session_store.rewrite_transcript,
            session_entry.session_id,
            history[:last_user_idx],
        )
        session_entry.last_prompt_tokens = 0

        preview = removed_msg[:40] + "..." if len(removed_msg) > 40 else removed_msg
        return t("gateway.undo.removed", count=removed_count, preview=preview)

    async def handle_suggestions_command(self, event: MessageEvent) -> str:
        """Delegate /suggestions to the shared CLI handler with gateway origin."""

        args = (event.get_command_args() or "").strip()
        source = event.source
        origin = None
        try:
            platform = getattr(source.platform, "value", None) or str(getattr(source, "platform", "") or "")
            chat_id = getattr(source, "chat_id", None)
            if platform and chat_id:
                origin = {
                    "platform": platform,
                    "chat_id": str(chat_id),
                    "chat_name": getattr(source, "chat_name", None),
                    "thread_id": getattr(source, "thread_id", None),
                }
        except Exception:
            origin = None
        try:
            from hermes_cli.suggestions_cmd import handle_suggestions_command

            return handle_suggestions_command(args, origin=origin)
        except Exception as e:
            logger.debug("suggestions command failed: %s", e)
            return f"Suggestions command failed: {e}"


def conversation_editing_for(runner) -> GatewayConversationEditingCommandService:
    service = getattr(runner, "conversation_editing", None)
    if isinstance(service, GatewayConversationEditingCommandService):
        return service
    service = GatewayConversationEditingCommandService(runner)
    runner.conversation_editing = service
    return service
