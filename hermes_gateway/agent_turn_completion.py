"""Agent turn cleanup and final delivery state handling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from hermes_gateway.final_delivery_confirmation import (
    stream_confirmed_final_delivery,
)
from hermes_gateway.runtime_status_writer import runtime_status_for
from hermes_gateway.session_runtime_state import session_runtime_state_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentTurnCleanupContext:
    progress_task: asyncio.Task | None
    stream_task: asyncio.Task | None
    stream_consumer: Any
    interrupt_monitor: asyncio.Task
    tracking_task: asyncio.Task
    notify_task: asyncio.Task
    session_key: str | None
    run_generation: int


class AgentTurnCleanupRuntime:
    def __init__(self, runner) -> None:
        self._runner = runner

    async def cleanup(self, context: AgentTurnCleanupContext) -> None:
        self._cancel_initial_tasks(context)
        await self._flush_or_cancel_stream_task(context)
        context.tracking_task.cancel()
        self._release_session_runtime_state(context)
        await self._await_cancelled_tasks(context)

    def _cancel_initial_tasks(self, context: AgentTurnCleanupContext) -> None:
        if context.progress_task:
            context.progress_task.cancel()
        context.interrupt_monitor.cancel()
        context.notify_task.cancel()

    async def _flush_or_cancel_stream_task(
        self,
        context: AgentTurnCleanupContext,
    ) -> None:
        stream_task = context.stream_task
        if not stream_task:
            return
        if context.stream_consumer is None:
            await self._cancel_and_await(stream_task)
            return
        try:
            await asyncio.wait_for(stream_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self._cancel_and_await(stream_task)

    def _release_session_runtime_state(self, context: AgentTurnCleanupContext) -> None:
        if context.session_key:
            session_runtime_state_for(self._runner).release_running_agent_state(
                context.session_key,
                run_generation=context.run_generation,
            )
        if self._runner._draining:
            runtime_status_for(self._runner).update_runtime_status("draining")

    async def _await_cancelled_tasks(self, context: AgentTurnCleanupContext) -> None:
        for task in (
            context.progress_task,
            context.interrupt_monitor,
            context.tracking_task,
            context.notify_task,
        ):
            if not task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                continue

    async def _cancel_and_await(self, task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return


class AgentFinalDeliveryRuntime:
    def mark_stream_delivery_and_register_cleanup(
        self,
        *,
        response: Any,
        stream_consumer: Any,
        session_key: str | None,
        run_generation: int,
        tool_progress: Any,
    ) -> Any:
        self._mark_already_sent_if_stream_delivered(
            response=response,
            stream_consumer=stream_consumer,
            session_key=session_key,
        )
        tool_progress.register_cleanup_callback(
            response=response,
            session_key=session_key,
            run_generation=run_generation,
            loop=asyncio.get_running_loop(),
        )
        return response

    def _mark_already_sent_if_stream_delivered(
        self,
        *,
        response: Any,
        stream_consumer: Any,
        session_key: str | None,
    ) -> None:
        if not isinstance(response, dict) or response.get("failed"):
            return
        final_response = response.get("final_response") or ""
        is_empty_sentinel = not final_response or final_response == "(empty)"
        previewed = bool(response.get("response_previewed"))
        content_delivered = bool(
            stream_consumer
            and getattr(stream_consumer, "final_content_delivered", False)
        )
        streamed = stream_confirmed_final_delivery(
            stream_consumer,
            final_response,
            previewed=previewed,
        )
        transformed = bool(response.get("response_transformed"))
        if (
            not is_empty_sentinel
            and not transformed
            and streamed
        ):
            logger.info(
                "Suppressing normal final send for session %s: final delivery already confirmed "
                "(streamed=%s previewed=%s content_delivered=%s).",
                session_key or "?",
                streamed,
                previewed,
                content_delivered,
            )
            response["already_sent"] = True


def agent_turn_cleanup_for(runner) -> AgentTurnCleanupRuntime:
    return AgentTurnCleanupRuntime(runner)


def agent_final_delivery_for() -> AgentFinalDeliveryRuntime:
    return AgentFinalDeliveryRuntime()
