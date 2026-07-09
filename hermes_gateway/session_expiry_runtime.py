"""Session expiry finalization watcher."""

from __future__ import annotations

import asyncio
import logging
import time

from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL
from hermes_gateway.gateway_runtime_config import runtime_config_for

logger = logging.getLogger(__name__)


class GatewaySessionExpiryRuntimeService:
    def __init__(self, runner):
        self._runner = runner

    async def session_expiry_watcher(self, interval: int = 300):
        """Background task that finalizes expired sessions."""
        await asyncio.sleep(60)
        finalize_failures: dict[str, int] = {}
        max_finalize_retries = 3
        runner = self._runner
        while runner._running:
            try:
                runner.session_store._ensure_loaded()
                expired_entries = []
                for key, entry in list(runner.session_store._entries.items()):
                    if entry.expiry_finalized:
                        continue
                    if not runner.session_store._is_session_expired(entry):
                        continue
                    expired_entries.append((key, entry))

                if expired_entries:
                    platforms: dict[str, int] = {}
                    for key, _entry in expired_entries:
                        parts = key.split(":")
                        platform = parts[2] if len(parts) > 2 else "unknown"
                        platforms[platform] = platforms.get(platform, 0) + 1
                    platform_summary = ", ".join(
                        f"{platform}:{count}" for platform, count in sorted(platforms.items())
                    )
                    logger.info(
                        "Session expiry: %d sessions to finalize (%s)",
                        len(expired_entries), platform_summary,
                    )

                for key, entry in expired_entries:
                    try:
                        self.finalize_expired_session(key, entry)
                        finalize_failures.pop(entry.session_id, None)
                    except Exception as exc:
                        failures = finalize_failures.get(entry.session_id, 0) + 1
                        finalize_failures[entry.session_id] = failures
                        if failures >= max_finalize_retries:
                            logger.warning(
                                "Session finalize gave up after %d attempts for %s: %s. "
                                "Marking as finalized to prevent infinite retry loop.",
                                failures, entry.session_id, exc,
                            )
                            with runner.session_store._lock:
                                entry.expiry_finalized = True
                                runner.session_store._save()
                            finalize_failures.pop(entry.session_id, None)
                        else:
                            logger.debug(
                                "Session finalize failed (%d/%d) for %s: %s",
                                failures, max_finalize_retries, entry.session_id, exc,
                            )

                if expired_entries:
                    done = sum(1 for _, entry in expired_entries if entry.expiry_finalized)
                    failed = len(expired_entries) - done
                    if failed:
                        logger.info(
                            "Session expiry done: %d finalized, %d pending retry",
                            done, failed,
                        )
                    else:
                        logger.info("Session expiry done: %d finalized", done)

                self.sweep_idle_and_prune_sessions()
            except Exception as exc:
                logger.debug("Session expiry watcher error: %s", exc)

            for _ in range(interval):
                if not runner._running:
                    break
                await asyncio.sleep(1)

    def finalize_expired_session(self, key: str, entry) -> None:
        runner = self._runner
        try:
            from hermes_cli.plugins import invoke_hook

            parts = key.split(":")
            platform = parts[2] if len(parts) > 2 else ""
            invoke_hook(
                "on_session_finalize",
                session_id=entry.session_id,
                platform=platform,
            )
        except Exception:
            pass

        cached_agent = None
        cache_lock = getattr(runner, "_agent_cache_lock", None)
        if cache_lock is not None:
            with cache_lock:
                cached = runner._agent_cache.get(key)
                cached_agent = cached[0] if isinstance(cached, tuple) else cached if cached else None
        if cached_agent is None:
            cached_agent = runner._running_agents.get(key)
        if cached_agent and cached_agent is not AGENT_PENDING_SENTINEL:
            runner._cleanup_agent_resources(cached_agent)

        runner._evict_cached_agent(key)
        runner._session_model_overrides.pop(key, None)
        runtime_config_for(runner).set_session_reasoning_override(key, None)
        if hasattr(runner, "_pending_model_notes"):
            runner._pending_model_notes.pop(key, None)
        pending_approvals = getattr(runner, "_pending_approvals", None)
        if isinstance(pending_approvals, dict):
            pending_approvals.pop(key, None)
        update_prompt_pending = getattr(runner, "_update_prompt_pending", None)
        if isinstance(update_prompt_pending, dict):
            update_prompt_pending.pop(key, None)
        with runner.session_store._lock:
            entry.expiry_finalized = True
            runner.session_store._save()
        logger.debug("Session expiry finalized for %s", entry.session_id)

    def sweep_idle_and_prune_sessions(self) -> None:
        runner = self._runner
        try:
            idle_evicted = runner._sweep_idle_cached_agents()
            if idle_evicted:
                logger.info("Agent cache idle sweep: evicted %d agent(s)", idle_evicted)
        except Exception as exc:
            logger.debug("Idle agent sweep failed: %s", exc)

        last_prune_ts = getattr(runner, "_last_session_store_prune_ts", 0.0)
        prune_interval = 3600.0
        if time.time() - last_prune_ts <= prune_interval:
            return
        try:
            max_age = int(getattr(runner.config, "session_store_max_age_days", 0) or 0)
            if max_age > 0:
                pruned = runner.session_store.prune_old_entries(max_age)
                if pruned:
                    logger.info("SessionStore prune: dropped %d stale entries", pruned)
        except Exception as exc:
            logger.debug("SessionStore prune failed: %s", exc)
        runner._last_session_store_prune_ts = time.time()


def session_expiry_runtime_for(runner) -> GatewaySessionExpiryRuntimeService:
    service = getattr(runner, "session_expiry_runtime", None)
    if isinstance(service, GatewaySessionExpiryRuntimeService):
        return service
    service = GatewaySessionExpiryRuntimeService(runner)
    runner.session_expiry_runtime = service
    return service
