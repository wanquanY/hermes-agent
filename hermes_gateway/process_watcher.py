"""Gateway background-process watcher behavior."""

from __future__ import annotations

import asyncio
import logging

from channels.platforms.base_models import MessageEvent, MessageType
from hermes_gateway.config import Platform
from hermes_gateway.config_model import _BUILTIN_PLATFORM_VALUES
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.process_notifications import format_gateway_process_notification
from hermes_gateway.session import SessionSource
from hermes_gateway.session_key import parse_session_key

logger = logging.getLogger(__name__)


class GatewayProcessWatcherMixin:
    """Background process notification and async delegation watcher methods."""

    def _build_process_event_source(self, evt: dict):
        """Resolve the canonical source for a synthetic background-process event."""
        session_key = str(evt.get("session_key") or "").strip()
        derived_platform = ""
        derived_chat_type = ""
        derived_chat_id = ""

        if session_key:
            try:
                self.session_store._ensure_loaded()
                entry = self.session_store._entries.get(session_key)
                if entry and getattr(entry, "origin", None):
                    return entry.origin
            except Exception as exc:
                logger.debug(
                    "Synthetic process-event session-store lookup failed for %s: %s",
                    session_key,
                    exc,
                )

            cached_source = self._get_cached_session_source(session_key)
            if cached_source is not None:
                return cached_source

            parsed = parse_session_key(session_key)
            if parsed:
                derived_platform = parsed["platform"]
                derived_chat_type = parsed["chat_type"]
                derived_chat_id = parsed["chat_id"]

        platform_name = str(evt.get("platform") or derived_platform or "").strip().lower()
        chat_type = str(evt.get("chat_type") or derived_chat_type or "").strip().lower()
        chat_id = str(evt.get("chat_id") or derived_chat_id or "").strip()
        if not platform_name or not chat_type or not chat_id:
            return None

        try:
            platform = Platform(platform_name)
            if platform.value not in _BUILTIN_PLATFORM_VALUES:
                try:
                    from channels.platform_registry import platform_registry

                    if not platform_registry.is_registered(platform.value):
                        raise ValueError(platform_name)
                except Exception:
                    raise ValueError(platform_name)
        except Exception:
            logger.warning(
                "Synthetic process event has invalid platform metadata: %r",
                platform_name,
            )
            return None

        return SessionSource(
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=str(evt.get("thread_id") or "").strip() or None,
            user_id=str(evt.get("user_id") or "").strip() or None,
            user_name=str(evt.get("user_name") or "").strip() or None,
        )

    async def _inject_watch_notification(self, synth_text: str, evt: dict) -> None:
        """Deliver a watch-pattern notification as a status message."""
        source = self._build_process_event_source(evt)
        if not source:
            logger.warning(
                "Dropping watch notification with no routing metadata for process %s",
                evt.get("session_id", "unknown"),
            )
            return
        platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        adapter = None
        for p, a in self.adapters.items():
            if p.value == platform_name:
                adapter = a
                break
        if not adapter:
            return
        try:
            message_id = str(evt.get("message_id") or "").strip() or None
            metadata = self._thread_metadata_for_source(source, message_id)
            logger.info(
                "Watch pattern notification — sending for %s chat=%s thread=%s",
                platform_name,
                source.chat_id,
                source.thread_id,
            )
            await adapter.send(source.chat_id, synth_text, metadata=metadata)
        except Exception as e:
            logger.error("Watch notification delivery error: %s", e)

    def _enrich_async_delegation_routing(self, evt: dict) -> None:
        """Fill platform/chat_id/thread_id/chat_type on an async-delegation event."""
        if evt.get("platform"):
            return
        parsed = parse_session_key(evt.get("session_key", "") or "")
        if not parsed:
            return
        evt["platform"] = parsed.get("platform", "")
        evt["chat_type"] = parsed.get("chat_type", "")
        evt["chat_id"] = parsed.get("chat_id", "")
        if parsed.get("thread_id"):
            evt["thread_id"] = parsed["thread_id"]

    async def _async_delegation_watcher(self, interval: float = 2.0) -> None:
        """Drain async-delegation completions and inject them as new turns."""
        await asyncio.sleep(3)
        from tools.process_registry import process_registry as _pr

        while self._running:
            try:
                requeue = []
                async_events = []
                while not _pr.completion_queue.empty():
                    try:
                        evt = _pr.completion_queue.get_nowait()
                    except Exception:
                        break
                    if evt.get("type") == "async_delegation":
                        async_events.append(evt)
                    else:
                        requeue.append(evt)
                for evt in requeue:
                    _pr.completion_queue.put(evt)
                for evt in async_events:
                    self._enrich_async_delegation_routing(evt)
                    synth_text = format_gateway_process_notification(evt)
                    if not synth_text:
                        continue
                    try:
                        await self._inject_watch_notification(synth_text, evt)
                    except Exception as e:
                        logger.error("Async delegation injection error: %s", e)
            except Exception as e:
                logger.debug("Async delegation watcher error: %s", e)
            await asyncio.sleep(interval)

    async def _run_process_watcher(self, watcher: dict) -> None:
        """Periodically check a background process and push updates to the user."""
        from tools.process_registry import process_registry

        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        session_key = watcher.get("session_key", "")
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")
        thread_id = watcher.get("thread_id", "")
        user_id = watcher.get("user_id", "")
        user_name = watcher.get("user_name", "")
        message_id = str(watcher.get("message_id") or "").strip() or None
        agent_notify = watcher.get("notify_on_complete", False)
        notify_mode = runtime_config_for(self).load_background_notifications_mode()

        logger.debug(
            "Process watcher started: %s (every %ss, notify=%s, agent_notify=%s)",
            session_id,
            interval,
            notify_mode,
            agent_notify,
        )

        if notify_mode == "off" and not agent_notify:
            while True:
                await asyncio.sleep(interval)
                session = process_registry.get(session_id)
                if session is None or session.exited:
                    break
            logger.debug("Process watcher ended (silent): %s", session_id)
            return

        last_output_len = 0
        while True:
            await asyncio.sleep(interval)

            session = process_registry.get(session_id)
            if session is None:
                break

            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len

            if session.exited:
                from tools.process_registry import process_registry as _pr_check

                if agent_notify and not _pr_check.is_completion_consumed(session_id):
                    from tools.ansi_strip import strip_ansi

                    raw_output = strip_ansi(session.output_buffer) if session.output_buffer else ""
                    limit = 2000
                    if len(raw_output) > limit:
                        tail = raw_output[-limit:]
                        newline = tail.find("\n")
                        tail = tail[newline + 1:] if newline != -1 else tail
                        output = f"[… output truncated — showing last {len(tail)} chars]\n{tail}"
                    else:
                        output = raw_output
                    synth_text = (
                        f"[IMPORTANT: Background process {session_id} completed "
                        f"(exit code {session.exit_code}).\n"
                        f"Command: {session.command}\n"
                        f"Output:\n{output}]"
                    )
                    source = self._build_process_event_source(
                        {
                            "session_id": session_id,
                            "session_key": session_key,
                            "platform": platform_name,
                            "chat_id": chat_id,
                            "thread_id": thread_id,
                            "user_id": user_id,
                            "user_name": user_name,
                        }
                    )
                    if not source:
                        logger.warning(
                            "Dropping completion notification with no routing metadata for process %s",
                            session_id,
                        )
                        break

                    adapter = None
                    for p, a in self.adapters.items():
                        if p == source.platform:
                            adapter = a
                            break
                    if adapter and source.chat_id:
                        try:
                            synth_event = MessageEvent(
                                text=synth_text,
                                message_type=MessageType.TEXT,
                                source=source,
                                internal=True,
                                message_id=message_id,
                            )
                            logger.info(
                                "Process %s finished — injecting agent notification for session %s chat=%s thread=%s",
                                session_id,
                                session_key,
                                source.chat_id,
                                source.thread_id,
                            )
                            await adapter.handle_message(synth_event)
                        except Exception as e:
                            logger.error("Agent notify injection error: %s", e)
                    break

                should_notify = (
                    notify_mode in {"all", "result"}
                    or (notify_mode == "error" and session.exit_code not in {0, None})
                )
                if should_notify:
                    new_output = session.output_buffer[-1000:] if session.output_buffer else ""
                    message_text = (
                        f"[Background process {session_id} finished with exit code {session.exit_code}~ "
                        f"Here's the final output:\n{new_output}]"
                    )
                    adapter = None
                    for p, a in self.adapters.items():
                        if p.value == platform_name:
                            adapter = a
                            break
                    if adapter and chat_id:
                        try:
                            send_meta = {"thread_id": thread_id} if thread_id else None
                            await adapter.send(chat_id, message_text, metadata=send_meta)
                        except Exception as e:
                            logger.error("Watcher delivery error: %s", e)
                break

            if has_new_output and notify_mode == "all" and not agent_notify:
                new_output = session.output_buffer[-500:] if session.output_buffer else ""
                message_text = (
                    f"[Background process {session_id} is still running~ "
                    f"New output:\n{new_output}]"
                )
                adapter = None
                for p, a in self.adapters.items():
                    if p.value == platform_name:
                        adapter = a
                        break
                if adapter and chat_id:
                    try:
                        send_meta = {"thread_id": thread_id} if thread_id else None
                        await adapter.send(chat_id, message_text, metadata=send_meta)
                    except Exception as e:
                        logger.error("Watcher delivery error: %s", e)

        logger.debug("Process watcher ended: %s", session_id)
