"""Cron trigger-provider interface.

Schedulers decide when a due job fires. Execution and result delivery remain
owned by the shared cron orchestrator, so external providers cannot fork agent
construction or delivery semantics.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any


class CronScheduler(ABC):
    """Trigger provider that decides when due cron jobs are fired."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider identifier."""

    def is_available(self) -> bool:
        """Return whether this provider can run without making network calls."""
        return True

    @abstractmethod
    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
        **kwargs: Any,
    ) -> None:
        """Begin firing due jobs and honor ``stop_event`` for teardown."""

    def stop(self) -> None:
        """Eagerly release provider resources when necessary."""
        return None

    def on_jobs_changed(self) -> None:
        """Reconcile provider state after a successful job-store mutation."""
        return None

    def recover_interrupted(self) -> int:
        """Recover profile-local interrupted attempts."""
        from cron.executions import recover_interrupted_executions

        return recover_interrupted_executions()

    def fire_due(
        self,
        job_id: str,
        *,
        adapters: Any = None,
        loop: Any = None,
    ) -> bool:
        """Atomically claim and run one externally-triggered job."""
        from cron.executions import create_execution
        from cron.jobs import claim_job_for_fire, get_job
        from cron.scheduler import run_one_job

        if not claim_job_for_fire(job_id):
            return False
        job = get_job(job_id)
        if job is None:
            return False
        job["execution_id"] = create_execution(job_id, source=self.name)["id"]
        return run_one_job(job, adapters=adapters, loop=loop)

    def reconcile(self) -> None:
        """Converge an external scheduler toward the local desired state."""
        return None


def resolve_cron_scheduler() -> CronScheduler:
    """Resolve the configured scheduler, falling back safely to the built-in."""
    import logging

    logger = logging.getLogger("cron.scheduler_provider")
    name = ""
    try:
        from hermes_cli.config import cfg_get, load_config

        name = (cfg_get(load_config(), "cron", "provider", default="") or "").strip()
    except Exception:
        pass

    if not name or name in ("builtin", "in-process", "inprocess"):
        return InProcessCronScheduler()

    try:
        from plugins.cron_providers import load_cron_scheduler

        provider = load_cron_scheduler(name)
        if provider is None:
            logger.warning("cron.provider '%s' not found; using built-in ticker", name)
            return InProcessCronScheduler()
        if not provider.is_available():
            logger.warning("cron.provider '%s' not available; using built-in ticker", name)
            return InProcessCronScheduler()
        logger.info("Using cron scheduler provider: %s", provider.name)
        return provider
    except Exception as exc:
        logger.warning(
            "Failed to load cron.provider '%s' (%s); using built-in ticker", name, exc
        )
        return InProcessCronScheduler()


class InProcessCronScheduler(CronScheduler):
    """Default in-process ticker provider."""

    @property
    def name(self) -> str:
        return "builtin"

    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
        can_dispatch: Any = None,
        **_kwargs: Any,
    ) -> None:
        """Run the shared cron tick loop until stopped."""
        import logging

        from cron.jobs import record_ticker_heartbeat
        from cron.scheduler import tick as cron_tick

        logger = logging.getLogger("cron.scheduler_provider")
        logger.info("In-process cron scheduler started (interval=%ds)", interval)
        recovered = self.recover_interrupted()
        if recovered:
            logger.warning(
                "Marked %d interrupted cron execution(s) unknown after restart",
                recovered,
            )
        record_ticker_heartbeat()
        while not stop_event.is_set():
            ok = False
            try:
                if can_dispatch is not None and not can_dispatch():
                    logger.debug("Cron dispatch paused while gateway drains existing work")
                else:
                    cron_tick(
                        verbose=False,
                        adapters=adapters,
                        loop=loop,
                        sync=False,
                        can_dispatch=can_dispatch,
                    )
                ok = True
            except BaseException as exc:
                logger.error("Cron tick error: %s", exc, exc_info=True)
            record_ticker_heartbeat(success=ok)
            stop_event.wait(interval)
