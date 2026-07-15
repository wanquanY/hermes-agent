"""Async supervision tasks for a gateway agent run."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from hermes_gateway.runtime_status_writer import runtime_status_for
from hermes_gateway.session_runtime_state import session_runtime_state_for

logger = logging.getLogger(__name__)


class AgentRunSupervisor:
    """Owns helper tasks around one agent executor run."""

    def __init__(
        self,
        *,
        runner,
        source,
        session_key: str | None,
        run_generation: int | None,
        agent_holder: list[Any],
        stream_consumer_holder: list[Any],
        status_thread_metadata: dict[str, Any] | None,
        track_message_result: Callable[[Any], None],
        should_emit_long_running_notification: Callable[[str | None, Any, Any], bool],
        notify_interval: float | None,
    ) -> None:
        self._runner = runner
        self._source = source
        self._session_key = session_key
        self._run_generation = run_generation
        self._agent_holder = agent_holder
        self._stream_consumer_holder = stream_consumer_holder
        self._status_thread_metadata = status_thread_metadata
        self._track_message_result = track_message_result
        self._should_emit_long_running_notification = should_emit_long_running_notification
        self._notify_interval = notify_interval
        self._notify_start = time.time()

    def start_stream_consumer(self) -> asyncio.Task:
        return asyncio.create_task(self._run_stream_consumer())

    def start_agent_tracking(self) -> asyncio.Task:
        return asyncio.create_task(self._track_agent())

    def start_interrupt_monitor(self, interrupt_detected: asyncio.Event) -> asyncio.Task:
        return asyncio.create_task(self._monitor_for_interrupt(interrupt_detected))

    def start_long_running_notifications(
        self,
        executor_task_provider: Callable[[], asyncio.Future | None],
    ) -> asyncio.Task:
        return asyncio.create_task(self._notify_long_running(executor_task_provider))

    async def _run_stream_consumer(self) -> None:
        for _ in range(200):
            if self._stream_consumer_holder[0] is not None:
                await self._stream_consumer_holder[0].run()
                return
            await asyncio.sleep(0.05)

    async def _track_agent(self) -> None:
        while self._agent_holder[0] is None:
            await asyncio.sleep(0.05)
        if not self._session_key:
            return
        if (
            self._run_generation is not None
            and not session_runtime_state_for(self._runner).is_session_run_current(
                self._session_key,
                self._run_generation,
            )
        ):
            logger.info(
                "Skipping stale agent promotion for %s — generation %s is no longer current",
                self._session_key or "",
                self._run_generation,
            )
            return
        self._runner._running_agents[self._session_key] = self._agent_holder[0]
        if self._runner._draining:
            runtime_status_for(self._runner).update_runtime_status("draining")

    async def _monitor_for_interrupt(self, interrupt_detected: asyncio.Event) -> None:
        if not self._session_key:
            return

        while True:
            await asyncio.sleep(0.2)
            try:
                adapter = self._runner.adapters.get(self._source.platform)
                if not adapter:
                    continue
                if not (
                    hasattr(adapter, "has_pending_interrupt")
                    and adapter.has_pending_interrupt(self._session_key)
                ):
                    continue

                agent = self._agent_holder[0]
                if agent:
                    pending_event = (
                        adapter.peek_pending_message(self._session_key)
                        if hasattr(adapter, "peek_pending_message")
                        else None
                    )
                    pending_text = pending_event.text if pending_event else None
                    logger.debug("Interrupt detected from adapter, signaling agent...")
                    agent.interrupt(pending_text)
                    interrupt_detected.set()
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("monitor_for_interrupt error (will retry): %s", exc)

    async def _notify_long_running(
        self,
        executor_task_provider: Callable[[], asyncio.Future | None],
    ) -> None:
        if self._notify_interval is None:
            return
        adapter = self._runner.adapters.get(self._source.platform)
        if not adapter:
            return
        while True:
            await asyncio.sleep(self._notify_interval)
            executor_task = executor_task_provider()
            if not self._should_emit_long_running_notification(
                self._session_key,
                self._agent_holder[0],
                executor_task,
            ):
                break
            elapsed_mins = int((time.time() - self._notify_start) // 60)
            status_detail = self._activity_status_detail()
            try:
                result = await adapter.send(
                    self._source.chat_id,
                    f"⏳ Still working... ({elapsed_mins} min elapsed{status_detail})",
                    metadata=self._status_thread_metadata,
                )
                self._track_message_result(result)
            except Exception as exc:
                logger.debug("Long-running notification error: %s", exc)

    def _activity_status_detail(self) -> str:
        agent = self._agent_holder[0]
        if not agent or not hasattr(agent, "get_activity_summary"):
            return ""
        try:
            activity = agent.get_activity_summary()
        except Exception as exc:
            logger.debug("Could not build long-running activity summary: %s", exc)
            return ""
        parts = [f"iteration {activity['api_call_count']}/{activity['max_iterations']}"]
        if activity.get("current_tool"):
            parts.append(f"running: {activity['current_tool']}")
        else:
            parts.append(activity.get("last_activity_desc", ""))
        return " — " + ", ".join(parts)


def agent_run_supervisor_for(runner, **kwargs) -> AgentRunSupervisor:
    return AgentRunSupervisor(runner=runner, **kwargs)
