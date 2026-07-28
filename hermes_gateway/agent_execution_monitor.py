"""Executor waiting, backup interrupts, and inactivity timeout handling."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

INTERRUPT_REASON_TIMEOUT = "Execution timed out (inactivity)"


class AgentExecutionMonitor:
    POLL_INTERVAL = 5.0

    def __init__(
        self,
        *,
        runner,
        source,
        session_key: str | None,
        agent_holder: list[Any],
        result_holder: list[Any],
        tools_holder: list[Any],
        interrupt_detected: asyncio.Event,
        interrupt_monitor: asyncio.Task,
        status_thread_metadata: dict[str, Any] | None,
        agent_timeout: float | None,
        agent_warning: float | None,
    ) -> None:
        self._runner = runner
        self._source = source
        self._session_key = session_key
        self._agent_holder = agent_holder
        self._result_holder = result_holder
        self._tools_holder = tools_holder
        self._interrupt_detected = interrupt_detected
        self._interrupt_monitor = interrupt_monitor
        self._status_thread_metadata = status_thread_metadata
        self._agent_timeout = agent_timeout
        self._agent_warning = agent_warning
        self._warning_fired = False

    async def wait(self, executor_task: asyncio.Future) -> dict[str, Any]:
        if self._agent_timeout is None:
            return await self._wait_unlimited(executor_task)
        return await self._wait_with_inactivity_timeout(executor_task)

    async def _wait_unlimited(self, executor_task: asyncio.Future) -> dict[str, Any]:
        while True:
            done, _ = await asyncio.wait({executor_task}, timeout=self.POLL_INTERVAL)
            if done:
                return executor_task.result()
            await self._backup_interrupt_if_pending()

    async def _wait_with_inactivity_timeout(
        self,
        executor_task: asyncio.Future,
    ) -> dict[str, Any]:
        while True:
            done, _ = await asyncio.wait({executor_task}, timeout=self.POLL_INTERVAL)
            if done:
                return executor_task.result()

            idle_secs = self._agent_idle_seconds()
            await self._send_inactivity_warning_if_needed(idle_secs)
            if idle_secs >= self._agent_timeout:
                return self._timeout_response(idle_secs)
            await self._backup_interrupt_if_pending()

    async def _backup_interrupt_if_pending(self) -> None:
        if self._interrupt_detected.is_set() or not self._session_key:
            return
        adapter = self._runner.adapters.get(self._source.platform)
        agent = self._agent_holder[0]
        if not (
            adapter
            and agent
            and hasattr(adapter, "has_pending_interrupt")
            and adapter.has_pending_interrupt(self._session_key)
        ):
            return
        event = (
            adapter.peek_pending_message(self._session_key)
            if hasattr(adapter, "peek_pending_message")
            else None
        )
        pending_text = event.text if event else None
        if event is not None and self._runner._pending_event_audio_paths(event):
            pending_text, _ = await self._runner._transcribe_and_echo_pending_voice(
                event,
                adapter,
                self._source,
                pending_text or "",
                log_context="Voice-backup-interrupt",
                metadata=self._status_thread_metadata,
            )
        logger.info(
            "Backup interrupt detected for session %s (monitor task state: %s)",
            self._session_key,
            "done" if self._interrupt_monitor.done() else "running",
        )
        agent.interrupt(pending_text)
        self._interrupt_detected.set()

    async def _send_inactivity_warning_if_needed(self, idle_secs: float) -> None:
        if (
            self._warning_fired
            or self._agent_warning is None
            or idle_secs < self._agent_warning
        ):
            return
        self._warning_fired = True
        adapter = self._runner.adapters.get(self._source.platform)
        if not adapter:
            return
        elapsed_warn = int(self._agent_warning // 60) or 1
        remaining_mins = int((self._agent_timeout - self._agent_warning) // 60) or 1
        try:
            await adapter.send(
                self._source.chat_id,
                f"⚠️ No activity for {elapsed_warn} min. "
                f"If the agent does not respond soon, it will "
                f"be timed out in {remaining_mins} min. "
                f"You can continue waiting or use /reset.",
                metadata=self._status_thread_metadata,
            )
        except Exception as exc:
            logger.debug("Inactivity warning send error: %s", exc)

    def _timeout_response(self, idle_secs: float) -> dict[str, Any]:
        agent = self._agent_holder[0]
        activity = self._agent_activity_summary(agent)
        last_desc = activity.get("last_activity_desc", "unknown")
        secs_ago = activity.get("seconds_since_activity", idle_secs)
        current_tool = activity.get("current_tool")
        iter_n = activity.get("api_call_count", 0)
        iter_max = activity.get("max_iterations", 0)

        logger.error(
            "Agent idle for %.0fs (timeout %.0fs) in session %s | "
            "last_activity=%s | iteration=%s/%s | tool=%s",
            secs_ago,
            self._agent_timeout,
            self._session_key,
            last_desc,
            iter_n,
            iter_max,
            current_tool or "none",
        )

        if agent and hasattr(agent, "interrupt"):
            agent.interrupt(INTERRUPT_REASON_TIMEOUT)

        timeout_mins = int(self._agent_timeout // 60) or 1
        lines = [
            f"⏱️ Agent inactive for {timeout_mins} min — no tool calls or API responses."
        ]
        if current_tool:
            lines.append(
                f"The agent appears stuck on tool `{current_tool}` "
                f"({secs_ago:.0f}s since last activity, iteration {iter_n}/{iter_max})."
            )
        else:
            lines.append(
                f"Last activity: {last_desc} ({secs_ago:.0f}s ago, "
                f"iteration {iter_n}/{iter_max}). "
                "The agent may have been waiting on an API response."
            )
        lines.append(
            "To increase the limit, set agent.gateway_timeout in config.yaml "
            "(value in seconds, 0 = no limit) and restart the gateway.\n"
            "Try again, or use /reset to start fresh."
        )
        return {
            "final_response": "\n".join(lines),
            "messages": self._result_holder[0].get("messages", []) if self._result_holder[0] else [],
            "api_calls": iter_n,
            "tools": self._tools_holder[0] or [],
            "history_offset": 0,
            "failed": True,
        }

    def _agent_idle_seconds(self) -> float:
        activity = self._agent_activity_summary(self._agent_holder[0])
        return float(activity.get("seconds_since_activity", 0.0))

    def _agent_activity_summary(self, agent) -> dict[str, Any]:
        if agent and hasattr(agent, "get_activity_summary"):
            try:
                return agent.get_activity_summary()
            except Exception as exc:
                logger.debug("Could not read agent activity summary: %s", exc)
        return {}


def agent_execution_monitor_for(**kwargs) -> AgentExecutionMonitor:
    return AgentExecutionMonitor(**kwargs)
