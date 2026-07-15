"""Gateway active-session message routing."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from agent.i18n import t
from channels.platforms.base import (
    EphemeralReply,
    MessageEvent,
    MessageType,
    merge_pending_message_event,
)
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL
from hermes_gateway.bootstrap import float_env
from hermes_gateway.busy_session_runtime import busy_session_runtime_for
from hermes_gateway.config import Platform
from hermes_gateway.footer_command import footer_command_for
from hermes_gateway.goal_commands import goal_command_for
from hermes_gateway.restart_lifecycle import restart_lifecycle_for
from hermes_gateway.runtime_status_command import runtime_status_command_for
from hermes_gateway.session_runtime_state import session_runtime_state_for
from hermes_gateway.update_lifecycle import update_lifecycle_for
from hermes_gateway.verbose_command import verbose_command_for
from hermes_gateway.yolo_command import yolo_command_for

logger = logging.getLogger(__name__)

_INTERRUPT_REASON_STOP = "Stop requested"
_INTERRUPT_REASON_RESET = "Session reset requested"


@dataclass(frozen=True)
class BusyMessageResult:
    handled: bool
    response: Any = None


class GatewayBusyMessageService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_if_busy(self, event: MessageEvent, source, session_key: str) -> BusyMessageResult:
        self._evict_stale_running_agent(session_key)
        runner = self._runner
        if session_key not in runner._running_agents:
            return BusyMessageResult(handled=False)

        if event.get_command() == "status":
            return BusyMessageResult(
                handled=True,
                response=await runtime_status_command_for(runner).handle_status_command(event),
            )

        from hermes_cli.commands import (
            ACTIVE_SESSION_BYPASS_COMMANDS as dedicated_handlers,
            resolve_command as resolve_command,
        )

        event_command = event.get_command()
        cmd_def = resolve_command(event_command) if event_command else None

        if event_command and cmd_def is not None:
            from channels.slash_commands import check_slash_access

            denied = check_slash_access(
                gateway_config=runner.config,
                source=source,
                canonical_cmd=cmd_def.name,
            )
            if denied is not None:
                return BusyMessageResult(handled=True, response=denied)

        if cmd_def and cmd_def.name == "restart":
            return BusyMessageResult(
                handled=True,
                response=await restart_lifecycle_for(runner).handle_restart_command(event),
            )

        if cmd_def and cmd_def.name == "stop":
            await runner._interrupt_and_clear_session(
                session_key,
                source,
                interrupt_reason=_INTERRUPT_REASON_STOP,
                invalidation_reason="stop_command",
            )
            logger.info(
                "STOP for session %s — agent interrupted, session lock released",
                session_key,
            )
            return BusyMessageResult(
                handled=True,
                response=EphemeralReply(t("gateway.stop.stopped")),
            )

        if cmd_def and cmd_def.name == "new":
            await runner._interrupt_and_clear_session(
                session_key,
                source,
                interrupt_reason=_INTERRUPT_REASON_RESET,
                invalidation_reason="new_command",
            )
            return BusyMessageResult(
                handled=True,
                response=await runner._handle_reset_command(event),
            )

        if event.get_command() in {"queue", "q"}:
            return BusyMessageResult(
                handled=True,
                response=self._queue_followup(event, source, session_key),
            )

        if cmd_def and cmd_def.name == "steer":
            return BusyMessageResult(
                handled=True,
                response=self._handle_steer(event, source, session_key),
            )

        if cmd_def and cmd_def.name == "model":
            return BusyMessageResult(
                handled=True,
                response="Agent is running — wait or /stop first, then switch models.",
            )

        if cmd_def and cmd_def.name == "codex-runtime":
            return BusyMessageResult(
                handled=True,
                response="Agent is running — wait or /stop first, then change runtime.",
            )

        if cmd_def and cmd_def.name in {"approve", "deny"}:
            if cmd_def.name == "approve":
                return BusyMessageResult(
                    handled=True,
                    response=await runner._handle_approve_command(event),
                )
            return BusyMessageResult(
                handled=True,
                response=await runner._handle_deny_command(event),
            )

        if cmd_def and cmd_def.name == "agents":
            return BusyMessageResult(
                handled=True,
                response=await runtime_status_command_for(runner).handle_agents_command(event),
            )

        if cmd_def and cmd_def.name == "background":
            return BusyMessageResult(
                handled=True,
                response=await runner._handle_background_command(event),
            )

        if cmd_def and cmd_def.name == "kanban":
            from channels.slash_commands import handle_kanban_command

            return BusyMessageResult(
                handled=True,
                response=await handle_kanban_command(
                    event=event,
                    notifier_profile=getattr(runner, "_kanban_notifier_profile", None),
                    active_profile_name=runner._active_profile_name,
                ),
            )

        if cmd_def and cmd_def.name == "goal":
            goal_arg = (event.get_command_args() or "").strip().lower()
            if not goal_arg or goal_arg in {
                "status",
                "pause",
                "resume",
                "clear",
                "stop",
                "done",
            }:
                return BusyMessageResult(
                    handled=True,
                    response=await goal_command_for(runner).handle_goal_command(event),
                )
            return BusyMessageResult(
                handled=True,
                response=(
                    "Agent is running — use /goal status / pause / clear mid-run, "
                    "or /stop before setting a new goal."
                ),
            )

        if cmd_def and cmd_def.name == "subgoal":
            return BusyMessageResult(
                handled=True,
                response=await goal_command_for(runner).handle_subgoal_command(event),
            )

        if cmd_def and cmd_def.name in {"yolo", "verbose", "footer"}:
            if cmd_def.name == "yolo":
                return BusyMessageResult(
                    handled=True,
                    response=await yolo_command_for(runner).handle_yolo_command(event),
                )
            if cmd_def.name == "verbose":
                return BusyMessageResult(
                    handled=True,
                    response=await verbose_command_for(runner).handle_verbose_command(event),
                )
            if cmd_def.name == "footer":
                return BusyMessageResult(
                    handled=True,
                    response=await footer_command_for(runner).handle_footer_command(event),
                )

        if cmd_def and cmd_def.name in dedicated_handlers:
            if cmd_def.name == "help":
                return BusyMessageResult(
                    handled=True,
                    response=await runner._handle_help_command(event),
                )
            if cmd_def.name == "commands":
                return BusyMessageResult(
                    handled=True,
                    response=await runner._handle_commands_command(event),
                )
            if cmd_def.name == "profile":
                return BusyMessageResult(
                    handled=True,
                    response=await runner._handle_profile_command(event),
                )
            if cmd_def.name == "update":
                return BusyMessageResult(
                    handled=True,
                    response=await update_lifecycle_for(runner).handle_update_command(event),
                )

        if cmd_def:
            return BusyMessageResult(
                handled=True,
                response=(
                    f"⏳ Agent is running — `/{cmd_def.name}` can't run "
                    f"mid-turn. Wait for the current response or `/stop` first."
                ),
            )

        if event.message_type == MessageType.PHOTO:
            logger.debug(
                "PRIORITY photo follow-up for session %s — queueing without interrupt",
                session_key,
            )
            adapter = runner.adapters.get(source.platform)
            if adapter:
                merge_pending_message_event(adapter._pending_messages, session_key, event)
            return BusyMessageResult(handled=True, response=None)

        telegram_followup_grace = float(
            os.getenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")
        )
        started_at = runner._running_agents_ts.get(session_key, 0)
        if (
            source.platform == Platform.TELEGRAM
            and event.message_type == MessageType.TEXT
            and telegram_followup_grace > 0
            and started_at
            and (time.time() - started_at) <= telegram_followup_grace
        ):
            logger.debug(
                "Telegram follow-up arrived %.2fs after run start for %s — queueing without interrupt",
                time.time() - started_at,
                session_key,
            )
            adapter = runner.adapters.get(source.platform)
            if adapter:
                merge_pending_message_event(
                    adapter._pending_messages,
                    session_key,
                    event,
                    merge_text=True,
                )
            return BusyMessageResult(handled=True, response=None)

        running_agent = runner._running_agents.get(session_key)
        if running_agent is AGENT_PENDING_SENTINEL:
            if event.get_command() == "stop":
                session_runtime_state_for(runner).release_running_agent_state(session_key)
                logger.info("HARD STOP (pending) for session %s — sentinel cleared", session_key)
                return BusyMessageResult(
                    handled=True,
                    response=EphemeralReply(
                        "⚡ Force-stopped. The agent was still starting — session unlocked."
                    ),
                )
            adapter = runner.adapters.get(source.platform)
            if adapter:
                merge_pending_message_event(
                    adapter._pending_messages,
                    session_key,
                    event,
                    merge_text=True,
                )
            return BusyMessageResult(handled=True, response=None)

        if runner._draining:
            if busy_session_runtime_for(runner).queue_during_drain_enabled():
                busy_session_runtime_for(runner).queue_or_replace_pending_event(session_key, event)
            response = (
                f"⏳ Gateway {runner._status_action_gerund()} — queued for the next turn after it comes back."
                if busy_session_runtime_for(runner).queue_during_drain_enabled()
                else f"⏳ Gateway is {runner._status_action_gerund()} and is not accepting another turn right now."
            )
            return BusyMessageResult(handled=True, response=response)

        if runner._busy_input_mode == "queue":
            logger.debug("PRIORITY queue follow-up for session %s", session_key)
            busy_session_runtime_for(runner).queue_or_replace_pending_event(session_key, event)
            return BusyMessageResult(handled=True, response=None)

        if runner._busy_input_mode == "steer":
            steer_text = (event.text or "").strip()
            steered = False
            if steer_text and hasattr(running_agent, "steer"):
                try:
                    steered = bool(running_agent.steer(steer_text))
                except Exception as exc:
                    logger.warning("PRIORITY steer failed for session %s: %s", session_key, exc)
                    steered = False
            if steered:
                logger.debug("PRIORITY steer for session %s", session_key)
                return BusyMessageResult(handled=True, response=None)
            logger.debug("PRIORITY steer-fallback-to-queue for session %s", session_key)
            busy_session_runtime_for(runner).queue_or_replace_pending_event(session_key, event)
            return BusyMessageResult(handled=True, response=None)

        logger.debug("PRIORITY interrupt for session %s", session_key)
        running_agent.interrupt(event.text)
        return BusyMessageResult(handled=True, response=None)

    def _evict_stale_running_agent(self, session_key: str) -> None:
        runner = self._runner
        raw_stale_timeout = float_env("HERMES_AGENT_TIMEOUT", 1800)
        stale_ts = runner._running_agents_ts.get(session_key, 0)
        if session_key not in runner._running_agents or not stale_ts:
            return

        stale_age = time.time() - stale_ts
        stale_agent = runner._running_agents.get(session_key)
        stale_idle = float("inf")
        stale_detail = ""
        if stale_agent and hasattr(stale_agent, "get_activity_summary"):
            try:
                summary = stale_agent.get_activity_summary()
                stale_idle = summary.get("seconds_since_activity", float("inf"))
                stale_detail = (
                    f" | last_activity={summary.get('last_activity_desc', 'unknown')} "
                    f"({stale_idle:.0f}s ago) "
                    f"| iteration={summary.get('api_call_count', 0)}/{summary.get('max_iterations', 0)}"
                )
            except Exception as exc:
                logger.debug("Could not read running-agent activity summary: %s", exc)

        wall_ttl = max(raw_stale_timeout * 10, 7200) if raw_stale_timeout > 0 else float("inf")
        should_evict = (
            stale_agent is not AGENT_PENDING_SENTINEL
            and (
                (raw_stale_timeout > 0 and stale_idle >= raw_stale_timeout)
                or stale_age > wall_ttl
            )
        )
        if not should_evict:
            return

        logger.warning(
            "Evicting stale _running_agents entry for %s "
            "(age: %.0fs, idle: %.0fs, timeout: %.0fs)%s",
            session_key,
            stale_age,
            stale_idle,
            raw_stale_timeout,
            stale_detail,
        )
        session_runtime_state_for(runner).invalidate_session_run_generation(
            session_key,
            reason="stale_running_agent_eviction",
        )
        session_runtime_state_for(runner).release_running_agent_state(session_key)

    def _queue_followup(self, event: MessageEvent, source, session_key: str) -> str:
        runner = self._runner
        queued_text = event.get_command_args().strip()
        if not queued_text:
            return "Usage: /queue <prompt>"
        adapter = runner.adapters.get(source.platform)
        if adapter:
            queued_event = MessageEvent(
                text=queued_text,
                message_type=MessageType.TEXT,
                source=event.source,
                message_id=event.message_id,
                channel_prompt=event.channel_prompt,
            )
            busy_session_runtime_for(runner).enqueue_fifo(session_key, queued_event, adapter)
        depth = busy_session_runtime_for(runner).queue_depth(
            session_key,
            adapter=runner.adapters.get(source.platform),
        )
        if depth <= 1:
            return "Queued for the next turn."
        return f"Queued for the next turn. ({depth} queued)"

    def _handle_steer(self, event: MessageEvent, source, session_key: str) -> str:
        runner = self._runner
        steer_text = event.get_command_args().strip()
        if not steer_text:
            return "Usage: /steer <prompt>"
        running_agent = runner._running_agents.get(session_key)
        if running_agent is AGENT_PENDING_SENTINEL:
            self._queue_pending_text(event, source, session_key, steer_text)
            return "Agent still starting — /steer queued for the next turn."
        if running_agent and hasattr(running_agent, "steer"):
            try:
                accepted = running_agent.steer(steer_text)
            except Exception as exc:
                logger.warning("Steer failed for session %s: %s", session_key, exc)
                return f"⚠️ Steer failed: {exc}"
            if accepted:
                preview = steer_text[:60] + ("..." if len(steer_text) > 60 else "")
                return f"⏩ Steer queued — arrives after the next tool call: '{preview}'"
            return "Steer rejected (empty payload)."
        self._queue_pending_text(event, source, session_key, steer_text)
        return "No active agent — /steer queued for the next turn."

    def _queue_pending_text(self, event: MessageEvent, source, session_key: str, text: str) -> None:
        adapter = self._runner.adapters.get(source.platform)
        if not adapter:
            return
        queued_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=event.source,
            message_id=event.message_id,
            channel_prompt=event.channel_prompt,
        )
        adapter._pending_messages[session_key] = queued_event


def busy_message_for(runner) -> GatewayBusyMessageService:
    service = getattr(runner, "busy_message", None)
    if isinstance(service, GatewayBusyMessageService):
        return service
    service = GatewayBusyMessageService(runner)
    runner.busy_message = service
    return service
