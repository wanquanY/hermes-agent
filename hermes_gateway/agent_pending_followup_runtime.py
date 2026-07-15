"""Queued follow-up turn handling for gateway agent runs."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any

from channels.platforms.base_models import MessageEvent
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_gateway.busy_session_runtime import busy_session_runtime_for
from hermes_gateway.goal_commands import goal_command_for
from hermes_gateway.interrupt_control import is_control_interrupt_message
from hermes_gateway.media_context import build_media_placeholder
from hermes_gateway.pending_events import dequeue_pending_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingFollowupContext:
    message: str
    context_prompt: str
    history: list[dict[str, Any]]
    source: Any
    session_id: str
    session_key: str | None
    run_generation: int
    interrupt_depth: int
    status_thread_metadata: dict[str, Any] | None
    stream_consumer: Any
    stream_task: asyncio.Task | None


class AgentPendingFollowupRuntime:
    """Owns queued follow-up selection, delivery, and recursive turn launch."""

    def __init__(self, runner) -> None:
        self._runner = runner

    async def process(
        self,
        *,
        response: Any,
        result: dict[str, Any] | None,
        context: PendingFollowupContext,
    ) -> dict[str, Any] | None:
        if not result:
            return None
        adapter = self._runner.adapters.get(context.source.platform)
        pending_event, pending = self._select_pending_followup(
            adapter=adapter,
            result=result,
            session_key=context.session_key,
        )
        pending_event, pending = self._discard_command_followup(pending_event, pending)
        if self._runner._draining and (pending_event or pending):
            logger.info(
                "Discarding pending follow-up for session %s during gateway %s",
                context.session_key or "?",
                self._runner._status_action_label(),
            )
            pending_event = None
            pending = None
        if not (pending_event or pending):
            return None

        logger.debug("Processing pending message: '%s...'", (pending or "")[:40])
        if adapter and context.session_key and hasattr(adapter, "clear_pending_interrupt"):
            adapter.clear_pending_interrupt(context.session_key)

        if context.interrupt_depth >= self._runner._MAX_INTERRUPT_DEPTH:
            self._requeue_at_depth_cap(adapter, context.session_key, pending_event, pending)
            return result or {"final_response": response, "messages": context.history}

        if not result.get("interrupted"):
            await self._deliver_first_response_if_needed(adapter, result, context)
            await self._release_post_delivery_callback(adapter, context)

        updated_history = result.get("messages", context.history)
        next_source = context.source
        next_message = pending
        next_message_id = None
        next_channel_prompt = None
        if pending_event is not None:
            next_source = getattr(pending_event, "source", None) or context.source
            if (
                goal_command_for(self._runner).is_goal_continuation_event(pending_event)
                and not await run_sqlite_io(
                    goal_command_for(self._runner).goal_still_active_for_session,
                    context.session_id,
                )
            ):
                logger.info(
                    "Discarding stale goal continuation for session %s — goal is no longer active",
                    context.session_key or "?",
                )
                return result
            next_message = await self._runner._prepare_inbound_message_text(
                event=pending_event,
                source=next_source,
                history=updated_history,
            )
            if next_message is None:
                return result
            next_message_id = self._runner._reply_anchor_for_event(pending_event)
            next_channel_prompt = getattr(pending_event, "channel_prompt", None)

        await self._send_followup_typing(context)
        followup_result = await self._runner._run_agent(
            message=next_message,
            context_prompt=context.context_prompt,
            history=updated_history,
            source=next_source,
            session_id=context.session_id,
            session_key=context.session_key,
            run_generation=context.run_generation,
            _interrupt_depth=context.interrupt_depth + 1,
            event_message_id=next_message_id,
            channel_prompt=next_channel_prompt,
        )
        return _preserve_queued_followup_history_offset(result, followup_result)

    def _select_pending_followup(
        self,
        *,
        adapter: Any,
        result: dict[str, Any],
        session_key: str | None,
    ) -> tuple[MessageEvent | None, str | None]:
        pending_event = None
        pending = None
        if adapter and session_key:
            pending_event = dequeue_pending_event(adapter, session_key)
            pending_event = busy_session_runtime_for(self._runner).promote_queued_event(
                session_key,
                adapter,
                pending_event,
            )
            if (
                result.get("interrupted")
                and not pending_event
                and result.get("interrupt_message")
            ):
                interrupt_message = result.get("interrupt_message")
                if is_control_interrupt_message(interrupt_message):
                    logger.info(
                        "Ignoring control interrupt message for session %s: %s",
                        session_key or "?",
                        interrupt_message,
                    )
                else:
                    pending = interrupt_message
            elif pending_event:
                pending = pending_event.text or build_media_placeholder(pending_event)
                logger.debug(
                    "Processing queued message after agent completion: '%s...'",
                    pending[:40],
                )

        if not pending and not pending_event:
            leftover_steer = result.get("pending_steer")
            if leftover_steer:
                pending = leftover_steer
                logger.debug(
                    "Delivering leftover /steer as next turn: '%s...'",
                    pending[:40],
                )
        return pending_event, pending

    def _discard_command_followup(
        self,
        pending_event: MessageEvent | None,
        pending: str | None,
    ) -> tuple[MessageEvent | None, str | None]:
        if not pending or not pending.strip().startswith("/"):
            return pending_event, pending
        parts = pending.strip().split(None, 1)
        command_word = parts[0][1:].lower() if parts else ""
        if not command_word:
            return pending_event, pending
        try:
            from hermes_cli.commands import resolve_command

            if resolve_command(command_word):
                logger.info(
                    "Discarding command '/%s' from pending queue — commands must not be passed as agent input",
                    command_word,
                )
                return None, None
        except Exception as exc:
            logger.debug("Pending command resolution failed for /%s: %s", command_word, exc)
        return pending_event, pending

    def _requeue_at_depth_cap(
        self,
        adapter: Any,
        session_key: str | None,
        pending_event: MessageEvent | None,
        pending: str | None,
    ) -> None:
        logger.warning(
            "Interrupt recursion depth %d reached for session %s — queueing message instead of recursing.",
            self._runner._MAX_INTERRUPT_DEPTH,
            session_key,
        )
        if not adapter or not session_key:
            return
        if pending_event and hasattr(adapter, "queue_pending_message_event"):
            adapter.queue_pending_message_event(session_key, pending_event)
        elif pending and hasattr(adapter, "queue_message"):
            adapter.queue_message(session_key, pending)

    async def _deliver_first_response_if_needed(
        self,
        adapter: Any,
        result: dict[str, Any],
        context: PendingFollowupContext,
    ) -> None:
        stream_consumer = context.stream_consumer
        stream_task = context.stream_task
        if stream_consumer and stream_task:
            try:
                await asyncio.wait_for(stream_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                stream_task.cancel()
                try:
                    await stream_task
                except asyncio.CancelledError:
                    logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            except Exception as exc:
                logger.debug(
                    "Stream consumer wait before queued message failed: %s",
                    exc,
                )

        previewed = bool(result.get("response_previewed"))
        already_streamed = bool(
            (stream_consumer and getattr(stream_consumer, "final_response_sent", False))
            or previewed
            or (
                stream_consumer
                and getattr(stream_consumer, "final_content_delivered", False)
            )
        )
        first_response = result.get("final_response", "")
        if first_response and not already_streamed and adapter:
            try:
                logger.info(
                    "Queued follow-up for session %s: final stream delivery not confirmed; sending first response before continuing.",
                    context.session_key or "?",
                )
                await adapter.send(
                    context.source.chat_id,
                    first_response,
                    metadata=context.status_thread_metadata,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to send first response before queued message: %s",
                    exc,
                )
        elif first_response:
            logger.info(
                "Queued follow-up for session %s: skipping resend because final streamed delivery was confirmed.",
                context.session_key or "?",
            )

    async def _release_post_delivery_callback(
        self,
        adapter: Any,
        context: PendingFollowupContext,
    ) -> None:
        if not (
            adapter
            and context.session_key
            and hasattr(adapter, "pop_post_delivery_callback")
        ):
            return
        callback = adapter.pop_post_delivery_callback(
            context.session_key,
            generation=context.run_generation,
        )
        if not callable(callback):
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.debug("Post-delivery callback failed before queued follow-up: %s", exc)

    async def _send_followup_typing(self, context: PendingFollowupContext) -> None:
        adapter = self._runner.adapters.get(context.source.platform)
        if not adapter:
            return
        try:
            await adapter.send_typing(
                context.source.chat_id,
                metadata=context.status_thread_metadata,
            )
        except Exception as exc:
            logger.debug("Follow-up typing indicator failed: %s", exc)


def _preserve_queued_followup_history_offset(
    first_result: dict[str, Any] | None,
    followup_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(first_result, dict) or not isinstance(followup_result, dict):
        return followup_result
    if "history_offset" not in first_result or "history_offset" in followup_result:
        return followup_result
    followup_result["history_offset"] = first_result["history_offset"]
    return followup_result


def agent_pending_followup_for(runner) -> AgentPendingFollowupRuntime:
    return AgentPendingFollowupRuntime(runner)
