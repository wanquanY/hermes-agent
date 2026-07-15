"""Gateway shutdown and restart drain helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_gateway.config import Platform
from hermes_gateway.runtime_status_writer import runtime_status_for
from hermes_gateway.session_key import parse_session_key

logger = logging.getLogger(__name__)


class GatewayShutdownRuntimeMixin:
    async def _drain_active_agents(self, timeout: float) -> tuple[Dict[str, Any], bool]:
        snapshot = self._snapshot_running_agents()
        last_active_count = self._running_agent_count()
        last_status_at = 0.0

        def maybe_update_status(force: bool = False) -> None:
            nonlocal last_active_count, last_status_at
            now = asyncio.get_running_loop().time()
            active_count = self._running_agent_count()
            if force or active_count != last_active_count or (now - last_status_at) >= 1.0:
                runtime_status_for(self).update_runtime_status("draining")
                last_active_count = active_count
                last_status_at = now

        if not self._running_agents:
            maybe_update_status(force=True)
            return snapshot, False

        maybe_update_status(force=True)
        if timeout <= 0:
            return snapshot, True

        deadline = asyncio.get_running_loop().time() + timeout
        while self._running_agents and asyncio.get_running_loop().time() < deadline:
            maybe_update_status()
            await asyncio.sleep(0.1)
        timed_out = bool(self._running_agents)
        maybe_update_status(force=True)
        return snapshot, timed_out

    async def _notify_active_sessions_of_shutdown(self) -> None:
        """Send shutdown/restart notifications to active chats and home channels."""
        active = self._snapshot_running_agents()

        action = "restarting" if self._restart_requested else "shutting down"
        hint = (
            "Your current task will be interrupted. "
            "Send any message after restart and I'll try to resume where you left off."
            if self._restart_requested
            else "Your current task will be interrupted."
        )
        msg = f"⚠️ Gateway {action} — {hint}"

        notified: set[tuple[str, str, Optional[str]]] = set()
        for session_key in active:
            source = await run_sqlite_io(
                self._shutdown_notification_source,
                session_key,
            )
            if source is not None:
                platform_str = source.platform.value
                chat_id = str(source.chat_id)
                thread_id = source.thread_id
            else:
                parsed = parse_session_key(session_key)
                if not parsed:
                    continue
                platform_str = parsed["platform"]
                chat_id = parsed["chat_id"]
                thread_id = parsed.get("thread_id")

            dedup_key = (platform_str, chat_id, str(thread_id) if thread_id else None)
            if dedup_key in notified:
                continue

            try:
                platform = Platform(platform_str)
                adapter = self.adapters.get(platform)
                if not adapter:
                    continue

                platform_cfg = self.config.platforms.get(platform)
                if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                    logger.info(
                        "Shutdown notification suppressed for active session: %s has gateway_restart_notification=false",
                        platform_str,
                    )
                    continue

                metadata = {"thread_id": thread_id} if thread_id else None
                result = await adapter.send(chat_id, msg, metadata=metadata)
                if result is not None and getattr(result, "success", True) is False:
                    logger.debug(
                        "Failed to send shutdown notification to %s:%s: %s",
                        platform_str,
                        chat_id,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue

                notified.add(dedup_key)
                logger.info("Sent shutdown notification to active chat %s:%s", platform_str, chat_id)
            except Exception as exc:
                logger.debug(
                    "Failed to send shutdown notification to %s:%s: %s",
                    platform_str,
                    chat_id,
                    exc,
                )

        for platform, adapter in list(self.adapters.items()):
            home = self.config.get_home_channel(platform)
            if not home or not home.chat_id:
                continue

            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Shutdown notification suppressed for home channel: %s has gateway_restart_notification=false",
                    platform.value,
                )
                continue

            dedup_key = (platform.value, str(home.chat_id), str(home.thread_id) if home.thread_id else None)
            if dedup_key in notified:
                continue

            try:
                metadata = {"thread_id": home.thread_id} if home.thread_id else None
                if metadata:
                    result = await adapter.send(str(home.chat_id), msg, metadata=metadata)
                else:
                    result = await adapter.send(str(home.chat_id), msg)
                if result is not None and getattr(result, "success", True) is False:
                    logger.debug(
                        "Failed to send shutdown notification to home channel %s:%s: %s",
                        platform.value,
                        home.chat_id,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue

                notified.add(dedup_key)
                logger.info("Sent shutdown notification to home channel %s:%s", platform.value, home.chat_id)
            except Exception as exc:
                logger.debug(
                    "Failed to send shutdown notification to home channel %s:%s: %s",
                    platform.value,
                    home.chat_id,
                    exc,
                )

    def _shutdown_notification_source(self, session_key: str):
        try:
            if getattr(self, "session_store", None) is not None:
                self.session_store._ensure_loaded()
                entry = self.session_store._entries.get(session_key)
                source = getattr(entry, "origin", None) if entry else None
                if source is not None:
                    return source
        except Exception as exc:
            logger.debug(
                "Failed to load session origin for shutdown notification %s: %s",
                session_key,
                exc,
            )
        return self._get_cached_session_source(session_key)

    def _finalize_shutdown_agents(self, active_agents: Dict[str, Any]) -> None:
        for agent in active_agents.values():
            try:
                flush = getattr(agent, "_flush_messages_to_session_db", None)
                session_messages = getattr(agent, "_session_messages", None)
                if callable(flush) and isinstance(session_messages, list) and session_messages:
                    strip = getattr(agent, "_drop_trailing_empty_response_scaffolding", None)
                    if callable(strip):
                        try:
                            strip(session_messages)
                        except Exception:
                            logger.debug("Suppressed recoverable gateway exception", exc_info=True)
                    flush(session_messages)
            except Exception as exc:
                logger.debug("Shutdown transcript flush failed: %s", exc)
            try:
                from hermes_cli.plugins import invoke_hook

                invoke_hook(
                    "on_session_finalize",
                    session_id=getattr(agent, "session_id", None),
                    platform="gateway",
                )
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            self._cleanup_agent_resources(agent)

    def _cleanup_agent_resources(self, agent: Any) -> None:
        """Best-effort cleanup for temporary or cached agent instances."""
        if agent is None:
            return
        try:
            if hasattr(agent, "shutdown_memory_provider"):
                session_messages = getattr(agent, "_session_messages", None)
                if isinstance(session_messages, list):
                    agent.shutdown_memory_provider(session_messages)
                else:
                    agent.shutdown_memory_provider()
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)
        try:
            if hasattr(agent, "close"):
                agent.close()
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)
        try:
            from agent.auxiliary_client import cleanup_stale_async_clients

            cleanup_stale_async_clients()
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)
