"""Telegram long-poll lifecycle and recovery state machine.

Connection establishment remains in :mod:`telegram_connection`; this module
owns only getUpdates health, recovery, and teardown fencing.  A polling
generation is healthy only after the dedicated getUpdates request succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextvars import ContextVar
from typing import Any, Optional

from channels.platforms.telegram_security import redact_telegram_error

logger = logging.getLogger(__name__)

_UPDATER_STOP_TIMEOUT = 15.0
_UPDATER_START_TIMEOUT = 30.0
_DRAIN_TIMEOUT = 15.0
_POLLING_ERROR_TASK_STUCK_TIMEOUT = 300.0
_POLLING_PROGRESS_TIMEOUT = 60.0
_POLLING_GENERATION_CONTEXT: ContextVar[Optional[int]] = ContextVar(
    "telegram_polling_generation",
    default=None,
)


class _PollingLifecycleAbort(RuntimeError):
    """Internal control flow for polling startup fenced by teardown."""


def _runtime_constant(name: str, default: float) -> float:
    """Read a public compatibility constant, allowing deterministic tests."""
    public_module = sys.modules.get("channels.platforms.telegram")
    return float(getattr(public_module, name, default))


class TelegramPollingMixin:
    """Generation-aware getUpdates health and bounded recovery."""

    async def _drain_polling_connections(self) -> None:
        """Boundedly rebuild only the dedicated getUpdates request pool."""
        app = self._app
        if not (app and app.bot):
            return
        try:
            polling_request = app.bot._request[0]  # noqa: SLF001
        except Exception:
            return

        timeout = _runtime_constant("_DRAIN_TIMEOUT", _DRAIN_TIMEOUT)
        try:
            await asyncio.wait_for(polling_request.shutdown(), timeout=timeout)
        except Exception as error:
            logger.debug(
                "[%s] Polling request shutdown failed/timed out (non-fatal): %s",
                self.name,
                redact_telegram_error(error),
            )
        try:
            await asyncio.wait_for(polling_request.initialize(), timeout=timeout)
            logger.debug("[%s] Polling request pool drained", self.name)
        except Exception as error:
            logger.debug(
                "[%s] Polling request initialize failed/timed out (non-fatal): %s",
                self.name,
                redact_telegram_error(error),
            )

    def _begin_polling_generation(self) -> tuple[int, asyncio.Event]:
        """Create the sole health event for a new polling generation."""
        if self._polling_teardown_started:
            self._polling_progress_accepting = False
            self._send_path_degraded = True
            return self._polling_generation, self._polling_progress_event

        verifier = self._polling_progress_verifier_task
        if verifier is not None and not verifier.done():
            verifier.cancel()
        self._polling_progress_verifier_task = None
        self._polling_generation += 1
        self._polling_progress_event = asyncio.Event()
        self._polling_progress_accepting = True
        self._send_path_degraded = True
        return self._polling_generation, self._polling_progress_event

    def _record_polling_progress(self, generation: int) -> None:
        """Accept successful getUpdates I/O from the current generation only."""
        if self._polling_teardown_started or not self._polling_progress_accepting:
            return
        if generation != self._polling_generation:
            return
        self._polling_progress_event.set()
        self._polling_network_error_count = 0
        self._polling_conflict_count = 0
        self._send_path_degraded = False

    def _observe_polling_request_result(
        self,
        request: Any,
        generation: Optional[int],
        result: Any,
    ) -> None:
        """Observe a getUpdates response without replacing PTB parsing."""
        try:
            status_code, payload = result
        except (TypeError, ValueError):
            return
        if generation is None or not 200 <= status_code < 300:
            return
        try:
            envelope = request.parse_json_payload(payload)
        except Exception:
            return
        if isinstance(envelope, dict) and envelope.get("ok") is True and "result" in envelope:
            self._record_polling_progress(generation)

    def _instrument_polling_request(self, request: Any) -> Any:
        """Instrument a slotted PTB request without assigning a read-only method."""
        adapter = self
        base_class = type(request)

        class _InstrumentedPollingRequest(base_class):
            __slots__ = ()

            async def do_request(self, *args, **kwargs):
                generation = _POLLING_GENERATION_CONTEXT.get()
                result = await super().do_request(*args, **kwargs)
                adapter._observe_polling_request_result(self, generation, result)
                return result

        request.__class__ = _InstrumentedPollingRequest
        return request

    async def _start_polling_once(
        self,
        app: Any,
        *,
        drop_pending_updates: bool,
        error_callback: Any,
    ) -> None:
        """Start one bounded, generation-scoped getUpdates consumer."""
        if self._polling_teardown_started:
            raise _PollingLifecycleAbort("Telegram polling teardown started")
        generation, progress = self._begin_polling_generation()

        def generation_error_callback(error: Exception) -> None:
            if self._polling_teardown_started:
                return
            if generation != self._polling_generation:
                return
            if error_callback is not None:
                token = _POLLING_GENERATION_CONTEXT.set(None)
                try:
                    error_callback(error)
                finally:
                    _POLLING_GENERATION_CONTEXT.reset(token)

        token = _POLLING_GENERATION_CONTEXT.set(generation)
        try:
            await asyncio.wait_for(
                app.updater.start_polling(
                    allowed_updates=self._telegram_all_update_types(),
                    drop_pending_updates=drop_pending_updates,
                    error_callback=generation_error_callback,
                ),
                timeout=_runtime_constant(
                    "_UPDATER_START_TIMEOUT",
                    _UPDATER_START_TIMEOUT,
                ),
            )
        finally:
            _POLLING_GENERATION_CONTEXT.reset(token)

        if self._polling_teardown_started:
            self._polling_progress_accepting = False
            self._send_path_degraded = True
            raise _PollingLifecycleAbort("Telegram polling teardown started")
        self._schedule_polling_progress_verifier(generation, progress)

    @staticmethod
    def _telegram_all_update_types() -> Any:
        """Resolve PTB lazily so Telegram remains an optional dependency."""
        public_module = sys.modules.get("channels.platforms.telegram")
        update = getattr(public_module, "Update", None)
        return getattr(update, "ALL_TYPES", None)

    def _schedule_polling_progress_verifier(
        self,
        generation: int,
        progress: asyncio.Event,
    ) -> None:
        if self._polling_teardown_started:
            self._polling_progress_accepting = False
            self._send_path_degraded = True
            return
        previous = self._polling_progress_verifier_task
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.get_running_loop().create_task(
            self._verify_polling_after_reconnect(generation, progress)
        )
        self._polling_progress_verifier_task = task
        self._background_tasks.add(task)

        def clear(finished: asyncio.Task) -> None:
            self._background_tasks.discard(finished)
            if self._polling_progress_verifier_task is finished:
                self._polling_progress_verifier_task = None

        task.add_done_callback(clear)

    def _schedule_polling_recovery(self, error: Exception, *, reason: str) -> None:
        """Start one recovery owner while keeping the gateway process alive."""
        if self._polling_teardown_started or self.has_fatal_error:
            return
        task = self._polling_error_task
        if task is not None and not task.done():
            logger.debug(
                "[%s] Polling recovery already active; ignoring %s: %s",
                self.name,
                reason,
                redact_telegram_error(error),
            )
            return
        self._send_path_degraded = True
        logger.warning(
            "[%s] Telegram polling degraded (%s); retrying in background: %s",
            self.name,
            reason,
            redact_telegram_error(error),
        )
        task = asyncio.get_running_loop().create_task(
            self._handle_polling_network_error(error)
        )
        self._polling_error_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _delete_webhook_best_effort(self) -> bool:
        """Do not let a transient deleteWebhook failure kill polling startup."""
        delete_webhook = getattr(self._bot, "delete_webhook", None)
        if not callable(delete_webhook):
            return True
        try:
            await delete_webhook(drop_pending_updates=False)
            return True
        except Exception as error:
            if not self._looks_like_network_error(error):
                raise
            self._send_path_degraded = True
            logger.warning(
                "[%s] deleteWebhook hit a recoverable error; polling will retry: %s",
                self.name,
                redact_telegram_error(error),
            )
            return False

    async def _start_polling_resilient(
        self,
        *,
        drop_pending_updates: bool,
        error_callback: Any,
    ) -> bool:
        """Start polling or delegate transient bootstrap failure to recovery."""
        if self._polling_teardown_started:
            return False
        app = self._app
        if not (app and app.updater):
            raise RuntimeError("Telegram application/updater not initialized")
        try:
            await self._start_polling_once(
                app,
                drop_pending_updates=drop_pending_updates,
                error_callback=error_callback,
            )
            return True
        except _PollingLifecycleAbort:
            return False
        except Exception as error:
            if self._polling_teardown_started:
                return False
            if self._looks_like_polling_conflict(error):
                task = asyncio.get_running_loop().create_task(
                    self._handle_polling_conflict(error)
                )
                self._polling_error_task = task
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return False
            if self._looks_like_network_error(error):
                self._schedule_polling_recovery(error, reason="polling bootstrap")
                return False
            raise

    async def _handle_polling_network_error(self, error: Exception) -> None:
        """Run one bounded stop/drain/start step in the reconnect ladder."""
        if self._polling_teardown_started or self.has_fatal_error:
            return
        max_retries = 10
        self._polling_network_error_count += 1
        self._send_path_degraded = True
        attempt = self._polling_network_error_count
        if attempt > max_retries:
            message = (
                f"Telegram polling could not reconnect after {max_retries} "
                "network-error retries. Restarting gateway."
            )
            logger.error(
                "[%s] %s Last error: %s",
                self.name,
                message,
                redact_telegram_error(error),
            )
            self._set_fatal_error("telegram_network_error", message, retryable=True)
            await self._notify_fatal_error()
            return

        delay = min(5 * 2 ** (attempt - 1), 60)
        logger.warning(
            "[%s] Telegram network error (%d/%d), retrying in %ds: %s",
            self.name,
            attempt,
            max_retries,
            delay,
            redact_telegram_error(error),
        )
        await asyncio.sleep(delay)
        if self._polling_teardown_started:
            return

        app = self._app
        try:
            if app and app.updater and app.updater.running:
                try:
                    await asyncio.wait_for(
                        app.updater.stop(),
                        timeout=_runtime_constant(
                            "_UPDATER_STOP_TIMEOUT",
                            _UPDATER_STOP_TIMEOUT,
                        ),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] updater.stop() timed out; forcing pool drain",
                        self.name,
                    )
        except Exception as stop_error:
            logger.debug(
                "[%s] updater.stop() failed: %s",
                self.name,
                redact_telegram_error(stop_error),
            )

        if self._polling_teardown_started:
            return
        await self._drain_polling_connections()
        if self._polling_teardown_started:
            return

        try:
            if app is None:
                raise RuntimeError("Telegram application torn down during reconnect")
            await self._start_polling_once(
                app,
                drop_pending_updates=False,
                error_callback=self._polling_error_callback_ref,
            )
            logger.info(
                "[%s] Polling generation restarted; awaiting getUpdates progress",
                self.name,
            )
        except _PollingLifecycleAbort:
            return
        except Exception as retry_error:
            if self._polling_teardown_started:
                return
            logger.warning(
                "[%s] Telegram polling reconnect failed: %s",
                self.name,
                redact_telegram_error(retry_error),
            )
            if not self.has_fatal_error:
                task = asyncio.get_running_loop().create_task(
                    self._handle_polling_network_error(retry_error)
                )
                self._polling_error_task = task
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def _polling_heartbeat_loop(self) -> None:
        """Continuously detect dead consumers and wedged recovery owners."""
        stuck_task: Optional[asyncio.Task] = None
        stuck_since = 0.0
        while True:
            try:
                await asyncio.sleep(90)
                if self._polling_teardown_started or self.has_fatal_error:
                    return
                recovery = self._polling_error_task
                if recovery is not None and not recovery.done():
                    now = time.monotonic()
                    if recovery is not stuck_task:
                        stuck_task = recovery
                        stuck_since = now
                    elif now - stuck_since > _runtime_constant(
                        "_POLLING_ERROR_TASK_STUCK_TIMEOUT",
                        _POLLING_ERROR_TASK_STUCK_TIMEOUT,
                    ):
                        stuck_for = now - stuck_since
                        recovery.cancel()
                        message = (
                            f"Telegram reconnect task wedged for {stuck_for:.0f}s; "
                            "forcing gateway reconnect."
                        )
                        self._set_fatal_error(
                            "telegram_network_error",
                            message,
                            retryable=True,
                        )
                        await self._notify_fatal_error()
                        return
                else:
                    stuck_task = None

                bot = self._app.bot if self._app else None
                get_me = getattr(bot, "get_me", None)
                if bot is None:
                    continue
                if not callable(get_me):
                    return
                await asyncio.wait_for(get_me(), timeout=15)
                await self._probe_pending_updates(bot, 15)
            except asyncio.CancelledError:
                return
            except (asyncio.TimeoutError, OSError) as error:
                self._schedule_polling_recovery(error, reason="heartbeat probe")
            except Exception as error:
                if self._looks_like_network_error(error):
                    self._schedule_polling_recovery(error, reason="heartbeat probe")

    async def _probe_pending_updates(self, bot: Any, probe_timeout: float) -> None:
        """Detect a stopped updater or a queue its consumer is not draining."""
        if self._polling_teardown_started or self._webhook_mode:
            return
        recovery = self._polling_error_task
        if recovery is not None and not recovery.done():
            self._polling_not_running_count = 0
            return
        updater = getattr(self._app, "updater", None) if self._app else None
        if updater is None:
            self._polling_pending_stuck_count = 0
            return
        if not getattr(updater, "running", False):
            self._polling_pending_stuck_count = 0
            self._polling_not_running_count += 1
            if self._polling_not_running_count >= 2:
                self._polling_not_running_count = 0
                self._schedule_polling_recovery(
                    RuntimeError("Telegram updater stopped while polling"),
                    reason="heartbeat: updater stopped",
                )
            return

        self._polling_not_running_count = 0
        get_webhook_info = getattr(bot, "get_webhook_info", None)
        if not callable(get_webhook_info):
            return
        try:
            info = await asyncio.wait_for(get_webhook_info(), probe_timeout)
        except (asyncio.TimeoutError, OSError):
            return
        pending = int(getattr(info, "pending_update_count", 0) or 0)
        if pending <= 0:
            self._polling_pending_stuck_count = 0
            return
        self._polling_pending_stuck_count += 1
        if self._polling_pending_stuck_count >= 2:
            self._polling_pending_stuck_count = 0
            self._schedule_polling_recovery(
                RuntimeError("getUpdates queue is not draining"),
                reason="heartbeat: getUpdates consumer wedged",
            )

    async def _verify_polling_after_reconnect(
        self,
        generation: Optional[int] = None,
        progress: Optional[asyncio.Event] = None,
    ) -> None:
        """Require current-generation getUpdates progress before declaring health."""
        # Compatibility for direct callers predating generation tracking. The
        # lifecycle itself always supplies both arguments and uses the stronger
        # getUpdates health contract below.
        if generation is None and progress is None:
            await asyncio.sleep(_runtime_constant("_POLLING_PROGRESS_TIMEOUT", 60))
            if self._polling_teardown_started or self.has_fatal_error:
                return
            app = self._app
            if not (app and app.updater and app.updater.running):
                await self._handle_polling_network_error(
                    RuntimeError("Updater not running after reconnect")
                )
                return
            try:
                await asyncio.wait_for(app.bot.get_me(), timeout=10)
            except Exception as error:
                await self._handle_polling_network_error(error)
            return

        generation = self._polling_generation if generation is None else generation
        progress = self._polling_progress_event if progress is None else progress
        try:
            await asyncio.wait_for(
                progress.wait(),
                timeout=_runtime_constant(
                    "_POLLING_PROGRESS_TIMEOUT",
                    _POLLING_PROGRESS_TIMEOUT,
                ),
            )
        except asyncio.TimeoutError:
            pass
        if self._polling_teardown_started or progress.is_set() or self.has_fatal_error:
            return
        if not self._polling_progress_accepting:
            return
        if generation != self._polling_generation or progress is not self._polling_progress_event:
            return

        app = self._app
        if not (app and app.updater and app.updater.running):
            self._schedule_polling_recovery(
                RuntimeError("Updater made no getUpdates progress and stopped"),
                reason="progress verifier: updater stopped",
            )
            return
        try:
            await asyncio.wait_for(app.bot.get_me(), timeout=10)
        except Exception as error:
            if self._looks_like_network_error(error):
                self._schedule_polling_recovery(
                    error,
                    reason="progress verifier: connectivity failure",
                )
            else:
                logger.warning(
                    "[%s] Polling verifier hit a non-connectivity error: %s",
                    self.name,
                    redact_telegram_error(error),
                )
            return
        if (
            not self._polling_teardown_started
            and not progress.is_set()
            and generation == self._polling_generation
            and progress is self._polling_progress_event
        ):
            self._schedule_polling_recovery(
                RuntimeError("getUpdates made no progress before deadline"),
                reason="progress verifier: getUpdates stalled",
            )

    def _disarm_ptb_retry_loop(self) -> None:
        """Stop PTB's private retry loop before async recovery starts."""
        updater = getattr(self._app, "updater", None) if self._app else None
        if updater is None:
            return
        for attribute in (
            "_Updater__polling_task_stop_event",
            "_polling_task_stop_event",
        ):
            stop_event = getattr(updater, attribute, None)
            if isinstance(stop_event, asyncio.Event):
                stop_event.set()
                return

    async def _handle_polling_conflict(self, error: Exception) -> None:
        """Recover boundedly from stale or competing getUpdates sessions."""
        if self._polling_teardown_started:
            return
        if self.has_fatal_error and self.fatal_error_code == "telegram_polling_conflict":
            return
        self._polling_conflict_count += 1
        max_retries = 5
        delay = 10 + self._polling_conflict_count * 10
        if self._polling_conflict_count <= max_retries:
            logger.warning(
                "[%s] Telegram polling conflict (%d/%d), retrying in %ds: %s",
                self.name,
                self._polling_conflict_count,
                max_retries,
                delay,
                redact_telegram_error(error),
            )
            app = self._app
            try:
                if app and app.updater and app.updater.running:
                    await asyncio.wait_for(
                        app.updater.stop(),
                        timeout=_runtime_constant(
                            "_UPDATER_STOP_TIMEOUT",
                            _UPDATER_STOP_TIMEOUT,
                        ),
                    )
            except Exception as stop_error:
                logger.debug(
                    "[%s] Conflict stop failed: %s",
                    self.name,
                    redact_telegram_error(stop_error),
                )
            await asyncio.sleep(delay)
            if self._polling_teardown_started:
                return
            await self._drain_polling_connections()
            if self._polling_teardown_started:
                return
            try:
                if app is None:
                    raise RuntimeError("Telegram application torn down during reconnect")
                await self._start_polling_once(
                    app,
                    drop_pending_updates=False,
                    error_callback=self._polling_error_callback_ref,
                )
                return
            except _PollingLifecycleAbort:
                return
            except Exception as retry_error:
                if self._polling_teardown_started:
                    return
                if self._polling_conflict_count < max_retries:
                    task = asyncio.get_running_loop().create_task(
                        self._handle_polling_conflict(retry_error)
                    )
                    self._polling_error_task = task
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                    return

        if self._polling_teardown_started:
            return
        message = (
            f"Telegram polling could not recover after {max_retries} retries. "
            "Ensure no other process uses this bot token, then restart the gateway."
        )
        was_fatal = self.has_fatal_error
        self._set_fatal_error("telegram_polling_conflict", message, retryable=False)
        logger.error(
            "[%s] %s Original error: %s",
            self.name,
            message,
            redact_telegram_error(error),
        )
        try:
            if self._app and self._app.updater:
                await asyncio.wait_for(
                    self._app.updater.stop(),
                    timeout=_runtime_constant(
                        "_UPDATER_STOP_TIMEOUT",
                        _UPDATER_STOP_TIMEOUT,
                    ),
                )
        except Exception as stop_error:
            logger.debug(
                "[%s] Final conflict stop failed: %s",
                self.name,
                redact_telegram_error(stop_error),
            )
        if not was_fatal:
            await self._notify_fatal_error()

    async def _cancel_polling_lifecycle_tasks(self) -> None:
        """Cancel and join every task that may mutate polling state."""
        current = asyncio.current_task()
        tasks: list[asyncio.Task] = []
        seen: set[int] = set()
        for task in (
            self._polling_error_task,
            self._polling_progress_verifier_task,
            self._polling_heartbeat_task,
        ):
            if task is None or task.done() or task is current or id(task) in seen:
                continue
            seen.add(id(task))
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._polling_error_task is not current:
            self._polling_error_task = None
        if self._polling_progress_verifier_task is not current:
            self._polling_progress_verifier_task = None
        if self._polling_heartbeat_task is not current:
            self._polling_heartbeat_task = None

    def _fence_polling_teardown(self) -> None:
        """Invalidate callbacks from every generation before disconnect awaits."""
        self._polling_teardown_started = True
        self._polling_progress_accepting = False
        self._polling_generation += 1
        self._polling_progress_event = asyncio.Event()
        self._send_path_degraded = True
