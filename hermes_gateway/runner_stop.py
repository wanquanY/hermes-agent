"""Gateway runner shutdown owner."""

from __future__ import annotations

import asyncio
import logging
import time

from hermes_constants import get_hermes_home
from hermes_agent.composition.async_sqlite import (
    run_sqlite_io,
    shutdown_async_sqlite_boundary,
)
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL as _AGENT_PENDING_SENTINEL
from hermes_gateway.interrupt_control import (
    INTERRUPT_REASON_GATEWAY_RESTART as _INTERRUPT_REASON_GATEWAY_RESTART,
    INTERRUPT_REASON_GATEWAY_SHUTDOWN as _INTERRUPT_REASON_GATEWAY_SHUTDOWN,
)
from hermes_gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from hermes_gateway.runtime_status_writer import runtime_status_for

logger = logging.getLogger(__name__)
_hermes_home = get_hermes_home()


async def stop_gateway_runner(
    runner,
    *,
    restart: bool = False,
    detached_restart: bool = False,
    service_restart: bool = False,
) -> None:
    self = runner
    """Stop the gateway and disconnect all adapters."""
    if restart:
        self._restart_requested = True
        self._restart_detached = detached_restart
        self._restart_via_service = service_restart
    if self._stop_task is not None:
        await self._stop_task
        return

    async def _stop_impl() -> None:
        def _kill_tool_subprocesses(phase: str) -> None:
            """Kill tool subprocesses + tear down terminal envs + browsers.

            Called twice in the shutdown path: once eagerly after a
            drain timeout forces agent interrupt (so we reclaim bash/
            sleep children before systemd TimeoutStopSec escalates to
            SIGKILL on the cgroup — #8202), and once as a final
            catch-all at the end of _stop_impl() for the graceful
            path or anything respawned mid-teardown.

            All steps are best-effort; exceptions are swallowed so
            one subsystem's failure doesn't block the rest.
            """
            try:
                from tools.process_registry import process_registry
                _killed = process_registry.kill_all()
                if _killed:
                    logger.info(
                        "Shutdown (%s): killed %d tool subprocess(es)",
                        phase, _killed,
                    )
            except Exception as _e:
                logger.debug("process_registry.kill_all (%s) error: %s", phase, _e)
            try:
                from hermes_agent.application.subagent_execution_service import (
                    subagent_execution_runtime,
                )

                _async_n = subagent_execution_runtime.interrupt_all(
                    reason=f"gateway shutdown ({phase})"
                )
                if _async_n:
                    logger.info(
                        "Shutdown (%s): interrupted %d background delegation(s)",
                        phase, _async_n,
                    )
            except Exception as _e:
                logger.debug("async interrupt_all (%s) error: %s", phase, _e)
            try:
                from tools.terminal_tool import cleanup_all_environments
                cleanup_all_environments()
            except Exception as _e:
                logger.debug("cleanup_all_environments (%s) error: %s", phase, _e)
            try:
                from tools.browser_tool import cleanup_all_browsers
                cleanup_all_browsers()
            except Exception as _e:
                logger.debug("cleanup_all_browsers (%s) error: %s", phase, _e)

        logger.info(
            "Stopping gateway%s...",
            " for restart" if self._restart_requested else "",
        )
        _stop_started_at = time.monotonic()

        def _phase_elapsed() -> float:
            return time.monotonic() - _stop_started_at

        self._running = False
        self._draining = True

        # Notify all chats with active agents BEFORE draining.
        # Adapters are still connected here, so messages can be sent.
        await self._notify_active_sessions_of_shutdown()
        logger.info(
            "Shutdown phase: notify_active_sessions done at +%.2fs",
            _phase_elapsed(),
        )

        timeout = self._restart_drain_timeout

        # Pre-mark sessions as resume_pending BEFORE the drain wait.
        # If the process is killed by the service manager during the
        # drain, the durable marker is already written so the next
        # gateway boot can recover in-flight sessions (#27856).
        _pre_drain_keys: list[str] = []
        for _sk, _agent in list(self._running_agents.items()):
            if _agent is _AGENT_PENDING_SENTINEL:
                continue
            try:
                await run_sqlite_io(
                    self.session_store.mark_resume_pending,
                    _sk,
                    "restart_timeout" if self._restart_requested else "shutdown_timeout",
                )
                _pre_drain_keys.append(_sk)
            except Exception as _e:
                logger.debug("pre-drain mark_resume_pending failed for %s: %s", _sk, _e)

        _drain_started_at = time.monotonic()
        active_work_registry = getattr(self, "_active_work_registry", None)
        if active_work_registry is None:
            active_agents, timed_out = await self._drain_active_agents(timeout)
            active_work_remaining = self._running_agent_count()
        else:
            active_agents = self._snapshot_running_agents()
            drain_report = await active_work_registry.drain(
                timeout=timeout,
                cancel_grace=5.0,
            )
            # Crossing the graceful deadline is a non-clean shutdown even if
            # forced cancellation then releases every lease. Preserve resume
            # markers and skip the clean-shutdown marker in that case.
            timed_out = bool(drain_report.deadline_expired)
            active_work_remaining = len(drain_report.timed_out)
            if drain_report.callback_errors:
                logger.warning(
                    "Gateway active-work drain callback errors: %s",
                    drain_report.callback_errors,
                )
        logger.info(
            "Shutdown phase: drain done at +%.2fs (drain took %.2fs, "
            "timed_out=%s, active_at_start=%d, active_now=%d, active_work_now=%d)",
            _phase_elapsed(),
            time.monotonic() - _drain_started_at,
            timed_out,
            len(active_agents),
            self._running_agent_count(),
            active_work_remaining,
        )

        if not timed_out:
            # Drain completed gracefully — all running sessions finished.
            # Clear the pre-drain resume_pending markers so sessions that
            # completed during the drain window don't carry a stale flag.
            for _sk in _pre_drain_keys:
                if _sk not in self._running_agents:
                    try:
                        await run_sqlite_io(self.session_store.clear_resume_pending, _sk)
                    except Exception as _e:
                        logger.debug(
                            "clear_resume_pending after drain failed for %s: %s",
                            _sk, _e,
                        )

        if timed_out:
            logger.warning(
                "Gateway drain timed out after %.1fs with %d active agent(s); interrupting remaining work.",
                timeout,
                self._running_agent_count(),
            )
            # Mark forcibly-interrupted sessions as resume_pending BEFORE
            # interrupting the agents.  This preserves each session's
            # session_id + transcript so the next message on the same
            # session_key auto-resumes from the existing conversation
            # instead of getting routed through suspend_recently_active()
            # and converted into a fresh session.  Terminal escalation
            # for genuinely stuck sessions still flows through the
            # existing ``.restart_failure_counts`` stuck-loop counter
            # (incremented below, threshold 3), which sets
            # ``suspended=True`` and overrides resume_pending.
            #
            # Iterate self._running_agents (current) rather than the
            # drain-start ``active_agents`` snapshot — the snapshot
            # may include sessions that finished gracefully during
            # the drain window, and marking those falsely would give
            # them a stray restart-interruption system note on their
            # next turn even though their previous turn completed
            # cleanly.  Skip pending sentinels for the same reason
            # _interrupt_running_agents() does: their agent hasn't
            # started yet, there's nothing to interrupt, and the
            # session shouldn't carry a misleading resume flag.
            _resume_reason = (
                "restart_timeout" if self._restart_requested else "shutdown_timeout"
            )
            for _sk, _agent in list(self._running_agents.items()):
                if _agent is _AGENT_PENDING_SENTINEL:
                    continue
                try:
                    await run_sqlite_io(
                        self.session_store.mark_resume_pending,
                        _sk,
                        _resume_reason,
                    )
                except Exception as _e:
                    logger.debug(
                        "mark_resume_pending failed for %s: %s",
                        _sk, _e,
                    )
            self._interrupt_running_agents(
                _INTERRUPT_REASON_GATEWAY_RESTART if self._restart_requested else _INTERRUPT_REASON_GATEWAY_SHUTDOWN
            )
            interrupt_deadline = asyncio.get_running_loop().time() + 5.0
            while self._running_agents and asyncio.get_running_loop().time() < interrupt_deadline:
                runtime_status_for(self).update_runtime_status("draining")
                await asyncio.sleep(0.1)

            # Kill lingering tool subprocesses NOW, before we spend more
            # budget on adapter disconnect / session DB close.  Under
            # systemd (TimeoutStopSec bounded by drain_timeout+headroom),
            # deferring this to the end of stop() risks systemd escalating
            # to SIGKILL on the cgroup first — at which point bash/sleep
            # children left behind by an interrupted terminal tool get
            # killed by systemd instead of us (issue #8202).  The final
            # catch-all cleanup below still runs for the graceful path.
            _kill_tool_subprocesses("post-interrupt")
            logger.info(
                "Shutdown phase: post-interrupt tool kill done at +%.2fs",
                _phase_elapsed(),
            )

        if self._restart_requested and self._restart_detached:
            try:
                from hermes_gateway.restart_lifecycle import restart_lifecycle_for

                await restart_lifecycle_for(self).launch_detached_restart_command()
            except Exception as e:
                logger.error("Failed to launch detached gateway restart: %s", e)

        # Also shut down memory providers on idle cached agents.
        # _finalize_shutdown_agents only handles agents that were
        # mid-turn at drain time; the _agent_cache may still hold
        # idle agents whose MemoryProviders never received
        # on_session_end().
        _cache_lock = getattr(self, "_agent_cache_lock", None)
        _cache = getattr(self, "_agent_cache", None)
        if _cache_lock is not None and _cache is not None:
            with _cache_lock:
                _idle_agents = list(_cache.values())
                _cache.clear()
        else:
            _idle_agents = []

        def _finalize_all_agents() -> None:
            self._finalize_shutdown_agents(active_agents)
            for _entry in _idle_agents:
                _agent = _entry[0] if isinstance(_entry, tuple) else _entry
                self._cleanup_agent_resources(_agent)

        await asyncio.to_thread(_finalize_all_agents)

        for platform, adapter in list(self.adapters.items()):
            _adapter_started_at = time.monotonic()
            try:
                await adapter.cancel_background_tasks()
            except Exception as e:
                logger.debug("✗ %s background-task cancel error: %s", platform.value, e)
            try:
                await adapter.disconnect()
                logger.info(
                    "✓ %s disconnected (%.2fs)",
                    platform.value,
                    time.monotonic() - _adapter_started_at,
                )
            except Exception as e:
                logger.error(
                    "✗ %s disconnect error after %.2fs: %s",
                    platform.value,
                    time.monotonic() - _adapter_started_at,
                    e,
                )
        logger.info(
            "Shutdown phase: all adapters disconnected at +%.2fs",
            _phase_elapsed(),
        )

        for _task in list(self._background_tasks):
            if _task is self._stop_task:
                continue
            _task.cancel()
        self._background_tasks.clear()

        self.adapters.clear()
        self._running_agents.clear()
        self._running_agents_ts.clear()
        self._pending_messages.clear()
        self._pending_approvals.clear()
        if hasattr(self, '_busy_ack_ts'):
            self._busy_ack_ts.clear()
        self._shutdown_event.set()

        # Global cleanup: kill any remaining tool subprocesses not tied
        # to a specific agent (catch-all for zombie prevention). On the
        # drain-timeout path we already did this earlier after agent
        # interrupt — this second call catches (a) the graceful path
        # where drain succeeded without interrupt, and (b) anything
        # that got respawned between the earlier call and adapter
        # disconnect (defense in depth; safe to call repeatedly).
        _kill_tool_subprocesses("final-cleanup")
        logger.info(
            "Shutdown phase: final-cleanup tool kill done at +%.2fs",
            _phase_elapsed(),
        )

        # Reap the process-global auxiliary-client cache once at the very
        # end of teardown.  Per-turn cleanup runs in _cleanup_agent_resources
        # for each active agent, but clients bound to worker-thread loops
        # that died with their ThreadPoolExecutor (notably cron ticks) only
        # get swept here.  Without this, long-running gateways accumulate
        # async httpx transports until they hit EMFILE on macOS's default
        # RLIMIT_NOFILE=256.  See #14210.
        try:
            from agent.auxiliary_client import shutdown_cached_clients
            shutdown_cached_clients()
        except Exception as _e:
            logger.debug("shutdown_cached_clients error: %s", _e)

        # Close SQLite session DBs so the WAL write lock is released.
        # Without this, --replace and similar restart flows leave the
        # old gateway's connection holding the WAL lock until Python
        # actually exits — causing 'database is locked' errors when
        # the new gateway tries to open the same file.
        _close_targets = [
            getattr(self, "_session_db", None),
            getattr(self, "session_store", None),
        ]
        for _db_holder in (self, getattr(self, "session_store", None)):
            _legacy_db = getattr(_db_holder, "_db", None) if _db_holder else None
            if _legacy_db is not None:
                _close_targets.append(_legacy_db)
        _closed_ids: set[int] = set()
        for _target in _close_targets:
            close = getattr(_target, "close", None)
            if not callable(close) or id(_target) in _closed_ids:
                continue
            _closed_ids.add(id(_target))
            try:
                await run_sqlite_io(close)
            except Exception as _e:
                logger.debug("session store close error: %s", _e)
        logger.info(
            "Shutdown phase: session store close done at +%.2fs",
            _phase_elapsed(),
        )
        await shutdown_async_sqlite_boundary()

        from channels.runtime_status import remove_pid_file, release_gateway_runtime_lock
        remove_pid_file()
        release_gateway_runtime_lock()

        # Write a clean-shutdown marker so the next startup knows this
        # wasn't a crash.  suspend_recently_active() only needs to run
        # after unexpected exits.  However, if the drain timed out and
        # agents were force-interrupted, their sessions may be in an
        # incomplete state (trailing tool response, no final assistant
        # message).  Skip the marker in that case so the next startup
        # suspends those sessions — giving users a clean slate instead
        # of resuming a half-finished tool loop.
        if not timed_out:
            try:
                (_hermes_home / ".clean_shutdown").touch()
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)
        else:
            logger.info(
                "Skipping .clean_shutdown marker — drain timed out with "
                "interrupted agents; next startup will suspend recently "
                "active sessions."
            )

        # Track sessions that were active at shutdown for stuck-loop
        # detection (#7536).  On each restart, the counter increments
        # for sessions that were running.  If a session hits the
        # threshold (3 consecutive restarts while active), the next
        # startup auto-suspends it — breaking the loop.
        if active_agents:
            self._increment_restart_failure_counts(set(active_agents.keys()))

        if self._restart_requested and self._restart_via_service:
            self._exit_code = GATEWAY_SERVICE_RESTART_EXIT_CODE
            self._exit_reason = self._exit_reason or "Gateway restart requested"

        self._draining = False
        runtime_status_for(self).update_runtime_status("stopped", self._exit_reason)
        logger.info("Gateway stopped (total teardown %.2fs)", _phase_elapsed())

    self._stop_task = asyncio.create_task(_stop_impl())
    await self._stop_task
