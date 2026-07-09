"""Restart recovery and auto-resume session runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from channels.platforms.base import MessageEvent, MessageType
from hermes_constants import get_hermes_home
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL
from hermes_gateway.freshness import auto_continue_freshness_window
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_hermes_home = get_hermes_home()


class GatewaySessionRecoveryRuntimeMixin:
    _STUCK_LOOP_THRESHOLD = 3
    _STUCK_LOOP_FILE = ".restart_failure_counts"
    _AUTO_RESUME_REASONS = frozenset(
        {"restart_timeout", "shutdown_timeout", "restart_interrupted"}
    )

    def _increment_restart_failure_counts(self, active_session_keys: set) -> None:
        """Increment restart-failure counters for sessions active at shutdown."""
        path = _hermes_home / self._STUCK_LOOP_FILE
        try:
            counts = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            counts = {}

        new_counts = {}
        for key in active_session_keys:
            new_counts[key] = counts.get(key, 0) + 1

        try:
            atomic_json_write(path, new_counts, indent=None)
        except Exception:
            pass

    def _suspend_stuck_loop_sessions(self) -> int:
        """Suspend sessions that have been active across too many restarts."""
        path = _hermes_home / self._STUCK_LOOP_FILE
        if not path.exists():
            return 0

        try:
            counts = json.loads(path.read_text())
        except Exception:
            return 0

        suspended = 0
        stuck_keys = [k for k, v in counts.items() if v >= self._STUCK_LOOP_THRESHOLD]

        for session_key in stuck_keys:
            try:
                entry = self.session_store._entries.get(session_key)
                if entry and not entry.suspended:
                    entry.suspended = True
                    suspended += 1
                    logger.warning(
                        "Auto-suspended stuck session %s (active across %d "
                        "consecutive restarts — likely a stuck loop)",
                        session_key, counts[session_key],
                    )
            except Exception:
                pass

        if suspended:
            try:
                self.session_store._save()
            except Exception:
                pass

        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

        return suspended

    def _clear_restart_failure_count(self, session_key: str) -> None:
        """Clear the restart-failure counter for a session that completed OK."""
        path = _hermes_home / self._STUCK_LOOP_FILE
        if not path.exists():
            return
        try:
            counts = json.loads(path.read_text())
            if session_key in counts:
                del counts[session_key]
                if counts:
                    atomic_json_write(path, counts, indent=None)
                else:
                    path.unlink(missing_ok=True)
        except Exception:
            pass

    def _schedule_resume_pending_sessions(self) -> int:
        """Auto-continue fresh restart-interrupted sessions after startup."""
        window = auto_continue_freshness_window()
        try:
            with self.session_store._lock:
                self.session_store._ensure_loaded_locked()
                candidates = [
                    entry for entry in self.session_store._entries.values()
                    if entry.resume_pending
                    and not entry.suspended
                    and entry.origin is not None
                    and entry.resume_reason in self._AUTO_RESUME_REASONS
                ]
        except Exception as exc:
            logger.warning("Failed to enumerate resume-pending sessions: %s", exc)
            return 0

        now = datetime.now()
        scheduled = 0
        for entry in candidates:
            marker = entry.last_resume_marked_at or entry.updated_at
            if marker is not None and (now - marker).total_seconds() > window:
                continue

            source = entry.origin
            adapter = self.adapters.get(source.platform)
            if adapter is None:
                logger.debug(
                    "Skipping auto-resume for %s: adapter not ready for %s",
                    entry.session_key,
                    getattr(source.platform, "value", source.platform),
                )
                continue

            self._running_agents[entry.session_key] = AGENT_PENDING_SENTINEL
            self._running_agents_ts[entry.session_key] = time.time()
            self._persist_active_agents()

            event = MessageEvent(
                text="",
                message_type=MessageType.TEXT,
                source=source,
                internal=True,
            )
            task = asyncio.create_task(adapter.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            scheduled += 1

        if scheduled:
            logger.info(
                "Scheduled auto-resume for %d restart-interrupted session(s)",
                scheduled,
            )
        return scheduled
