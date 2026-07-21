"""Busy-session queue, steer, and acknowledgement runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from channels.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from hermes_constants import get_hermes_home, get_hermes_home_override
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL
from hermes_gateway.config import Platform

logger = logging.getLogger(__name__)
_hermes_home = get_hermes_home()


def _runtime_home():
    return get_hermes_home_override() or _hermes_home


def _load_gateway_config() -> dict:
    from hermes_agent.gateway.runtime_config import load_gateway_runtime_config

    return load_gateway_runtime_config(_runtime_home())


class GatewayBusySessionRuntimeService:
    def __init__(self, runner):
        self._runner = runner

    def queue_during_drain_enabled(self) -> bool:
        runner = self._runner
        return runner._restart_requested and runner._busy_input_mode in {"queue", "steer"}

    def enqueue_fifo(self, session_key: str, queued_event: MessageEvent, adapter: Any) -> None:
        """Append a /queue event to the FIFO chain for a session."""
        if adapter is None:
            return
        pending_slot = getattr(adapter, "_pending_messages", None)
        if pending_slot is None:
            return
        runner = self._runner
        queued_events = getattr(runner, "_queued_events", None)
        if queued_events is None:
            queued_events = {}
            runner._queued_events = queued_events
        if session_key in pending_slot:
            queued_events.setdefault(session_key, []).append(queued_event)
        else:
            pending_slot[session_key] = queued_event

    def promote_queued_event(
        self,
        session_key: str,
        adapter: Any,
        pending_event: Optional[MessageEvent],
    ) -> Optional[MessageEvent]:
        """Promote the next overflow item after the slot was drained."""
        queued_events = getattr(self._runner, "_queued_events", None)
        if not queued_events:
            return pending_event
        overflow = queued_events.get(session_key)
        if not overflow:
            return pending_event
        next_queued = overflow.pop(0)
        if not overflow:
            queued_events.pop(session_key, None)
        if pending_event is None:
            return next_queued
        if adapter is not None and hasattr(adapter, "_pending_messages"):
            adapter._pending_messages[session_key] = next_queued
        else:
            queued_events.setdefault(session_key, []).insert(0, next_queued)
        return pending_event

    def queue_depth(self, session_key: str, *, adapter: Any = None) -> int:
        """Total pending /queue items for a session: slot plus overflow."""
        queued_events = getattr(self._runner, "_queued_events", None) or {}
        depth = len(queued_events.get(session_key, []))
        if adapter is not None and session_key in getattr(adapter, "_pending_messages", {}):
            depth += 1
        return depth

    def queue_or_replace_pending_event(self, session_key: str, event: MessageEvent) -> None:
        adapter = self._runner._adapter_for_source(event.source)
        if not adapter:
            return
        pending = getattr(adapter, "_pending_messages", None)
        existing = pending.get(session_key) if isinstance(pending, dict) else None
        if existing is not None and (
            bool(getattr(existing, "media_urls", None))
            or bool(getattr(event, "media_urls", None))
            or getattr(existing, "message_type", None) != MessageType.TEXT
            or event.message_type != MessageType.TEXT
        ):
            merge_pending_message_event(
                adapter._pending_messages,
                session_key,
                event,
                merge_text=event.message_type == MessageType.TEXT,
            )
            return
        self.enqueue_fifo(session_key, event, adapter)

    async def compression_in_flight(self, running_agent: Any) -> bool:
        """Probe local and durable compression state, failing closed on I/O."""
        if running_agent is None or running_agent is AGENT_PENDING_SENTINEL:
            return False
        # Require the concrete boolean marker. Dynamic mocks/proxies can
        # manufacture a truthy attribute on demand, which must not silently
        # demote ordinary interrupts in either tests or production adapters.
        if getattr(running_agent, "_compression_in_flight", False) is True:
            return True
        codex_session = getattr(running_agent, "_codex_session", None)
        if getattr(codex_session, "is_compacting", False) is True:
            return True
        raw_session_id = getattr(running_agent, "session_id", "")
        if not isinstance(raw_session_id, str):
            return False
        session_id = raw_session_id.strip()
        leases = getattr(
            getattr(running_agent, "_session_db", None),
            "compression_leases",
            None,
        )
        # Attribute/type absence represents an old or deliberately narrow test
        # double, not an unknown production state.
        if not session_id or leases is None or not hasattr(leases, "holder"):
            return False
        try:
            return bool(await asyncio.to_thread(leases.holder, session_id))
        except (AttributeError, TypeError):
            return False
        except Exception:
            logger.warning(
                "Compression lease probe failed for session %s; treating "
                "compression as active to preserve the session boundary",
                session_id,
                exc_info=True,
            )
            return True

    async def handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:
        runner = self._runner
        if not runner._is_user_authorized(event.source):
            logger.warning(
                "Dropping message from unauthorized user in active session: "
                "user=%s (%s), platform=%s, session=%s",
                event.source.user_id,
                event.source.user_name,
                event.source.platform.value if event.source.platform else "unknown",
                session_key,
            )
            return True

        if runner._draining:
            return await self.handle_draining_busy_message(event, session_key)

        adapter = runner._adapter_for_source(event.source)
        if not adapter:
            return False

        running_agent = runner._running_agents.get(session_key)
        effective_mode = runner._busy_input_mode
        compression_demoted = False
        if (
            effective_mode == "interrupt"
            and await self.compression_in_flight(running_agent)
        ):
            logger.info(
                "Demoting busy interrupt to FIFO queue for session %s while "
                "context compression is in flight",
                session_key,
            )
            effective_mode = "queue"
            compression_demoted = True
        steered = False
        if effective_mode == "steer":
            steer_text = (event.text or "").strip()
            can_steer = (
                steer_text
                and running_agent is not None
                and running_agent is not AGENT_PENDING_SENTINEL
                and hasattr(running_agent, "steer")
            )
            if can_steer:
                try:
                    steered = bool(running_agent.steer(steer_text))
                except Exception as exc:
                    logger.warning("Gateway steer failed for session %s: %s", session_key, exc)
                    steered = False
            if not steered:
                effective_mode = "queue"

        if not steered:
            self.queue_or_replace_pending_event(session_key, event)

        is_queue_mode = effective_mode == "queue"
        is_steer_mode = effective_mode == "steer"

        if effective_mode == "interrupt" and running_agent and running_agent is not AGENT_PENDING_SENTINEL:
            try:
                interrupt_text = event.text or ""
                if runner._pending_event_audio_paths(event):
                    interrupt_text, _ = await runner._transcribe_and_echo_pending_voice(
                        event,
                        adapter,
                        event.source,
                        interrupt_text,
                        log_context="Voice-busy-interrupt",
                        metadata=runner._thread_metadata_for_source(
                            event.source,
                            runner._reply_anchor_for_event(event),
                        ),
                    )
                running_agent.interrupt(interrupt_text)
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        busy_ack_enabled = os.environ.get("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true").lower() == "true"
        if not busy_ack_enabled:
            logger.debug("Busy ack suppressed for session %s", session_key)
            return True

        busy_ack_cooldown = 30
        now = time.time()
        last_ack = runner._busy_ack_ts.get(session_key, 0)
        if now - last_ack < busy_ack_cooldown:
            return True

        runner._busy_ack_ts[session_key] = now

        status_detail = self.busy_status_detail(session_key, running_agent, now)
        if is_steer_mode:
            message = (
                f"⏩ Steered into current run{status_detail}. "
                f"Your message arrives after the next tool call."
            )
        elif compression_demoted:
            message = (
                f"🗜️ Compressing context safely{status_detail} — your message "
                "is queued for the next turn so it cannot interrupt the "
                "session boundary. Use /stop if you need to terminate the "
                "current task."
            )
        elif is_queue_mode:
            message = (
                f"⏳ Queued for the next turn{status_detail}. "
                f"I'll respond once the current task finishes."
            )
        else:
            message = (
                f"⚡ Interrupting current task{status_detail}. "
                f"I'll respond to your message shortly."
            )

        message = self.append_busy_onboarding_hint(
            message,
            is_steer_mode=is_steer_mode,
            is_queue_mode=is_queue_mode,
        )
        await self.send_busy_ack(event, adapter, message)
        return True

    async def handle_draining_busy_message(self, event: MessageEvent, session_key: str) -> bool:
        runner = self._runner
        adapter = runner._adapter_for_source(event.source)
        if not adapter:
            return True

        reply_anchor = runner._reply_anchor_for_event(event)
        thread_meta = runner._thread_metadata_for_source(event.source, reply_anchor)
        if self.queue_during_drain_enabled():
            self.queue_or_replace_pending_event(session_key, event)
            message = f"⏳ Gateway {runner._status_action_gerund()} — queued for the next turn after it comes back."
        else:
            message = f"⏳ Gateway is {runner._status_action_gerund()} and is not accepting another turn right now."

        await adapter._send_with_retry(
            chat_id=event.source.chat_id,
            content=message,
            reply_to=self.busy_reply_to(event, reply_anchor),
            metadata=thread_meta,
        )
        return True

    def busy_status_detail(self, session_key: str, running_agent: Any, now: float) -> str:
        status_parts = []
        if running_agent and running_agent is not AGENT_PENDING_SENTINEL:
            try:
                summary = running_agent.get_activity_summary()
                iteration = summary.get("api_call_count", 0)
                max_iter = summary.get("max_iterations", 0)
                current_tool = summary.get("current_tool")
                start_ts = self._runner._running_agents_ts.get(session_key, 0)
                if start_ts:
                    elapsed_min = int((now - start_ts) / 60)
                    if elapsed_min > 0:
                        status_parts.append(f"{elapsed_min} min elapsed")
                if max_iter:
                    status_parts.append(f"iteration {iteration}/{max_iter}")
                if current_tool:
                    status_parts.append(f"running: {current_tool}")
            except Exception as exc:
                logger.debug("Failed to summarize busy status for %s: %s", session_key, exc)
        return f" ({', '.join(status_parts)})" if status_parts else ""

    def append_busy_onboarding_hint(
        self,
        message: str,
        *,
        is_steer_mode: bool,
        is_queue_mode: bool,
    ) -> str:
        try:
            from agent.onboarding import (
                BUSY_INPUT_FLAG,
                busy_input_hint_gateway,
                is_seen,
                mark_seen,
            )

            user_config = _load_gateway_config()
            if is_seen(user_config, BUSY_INPUT_FLAG):
                return message
            if is_steer_mode:
                hint_mode = "steer"
            elif is_queue_mode:
                hint_mode = "queue"
            else:
                hint_mode = "interrupt"
            mark_seen(Path(_runtime_home()) / "config.yaml", BUSY_INPUT_FLAG)
            return f"{message}\n\n{busy_input_hint_gateway(hint_mode)}"
        except Exception as exc:
            logger.debug("Failed to apply busy-input onboarding hint: %s", exc)
            return message

    async def send_busy_ack(self, event: MessageEvent, adapter: Any, message: str) -> None:
        runner = self._runner
        reply_anchor = runner._reply_anchor_for_event(event)
        thread_meta = runner._thread_metadata_for_source(event.source, reply_anchor)
        try:
            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=message,
                reply_to=self.busy_reply_to(event, reply_anchor),
                metadata=thread_meta,
            )
        except Exception as exc:
            logger.debug("Failed to send busy-ack: %s", exc)

    @staticmethod
    def busy_reply_to(event: MessageEvent, reply_anchor):
        if (
            event.source.platform == Platform.TELEGRAM
            and event.source.chat_type == "dm"
            and event.source.thread_id
        ):
            return reply_anchor
        if event.source.platform == Platform.TELEGRAM and event.source.thread_id:
            return None
        return event.message_id


def busy_session_runtime_for(runner) -> GatewayBusySessionRuntimeService:
    service = getattr(runner, "busy_session_runtime", None)
    if isinstance(service, GatewayBusySessionRuntimeService):
        return service
    service = GatewayBusySessionRuntimeService(runner)
    runner.busy_session_runtime = service
    return service
