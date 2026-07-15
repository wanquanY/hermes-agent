"""Gateway cron ticker runtime."""

from __future__ import annotations

import logging
import threading

from agent.async_utils import safe_schedule_threadsafe
from channels.platforms.base import cleanup_document_cache, cleanup_image_cache

logger = logging.getLogger(__name__)


def start_cron_ticker(
    stop_event: threading.Event,
    adapters=None,
    loop=None,
    interval: int = 60,
) -> None:
    """Tick cron jobs and gateway maintenance tasks from a background thread."""

    from cron.scheduler import tick as cron_tick
    from hermes_cli.debug import _sweep_expired_pastes

    image_cache_every = 60
    channel_dir_every = 5
    paste_sweep_every = 60
    curator_every = 60

    logger.info("Cron ticker started (interval=%ds)", interval)
    tick_count = 0
    while not stop_event.is_set():
        try:
            cron_tick(verbose=False, adapters=adapters, loop=loop, sync=False)
        except Exception as exc:
            logger.debug("Cron tick error: %s", exc)

        tick_count += 1

        if tick_count % channel_dir_every == 0 and adapters:
            try:
                from hermes_gateway.channel_directory import build_channel_directory

                if loop is not None:
                    fut = safe_schedule_threadsafe(
                        build_channel_directory(adapters),
                        loop,
                        logger=logger,
                        log_message="Channel directory refresh scheduling error",
                    )
                    if fut is not None:
                        fut.result(timeout=30)
            except Exception as exc:
                logger.debug("Channel directory refresh error: %s", exc)

        if tick_count % image_cache_every == 0:
            try:
                removed = cleanup_image_cache(max_age_hours=24)
                if removed:
                    logger.info("Image cache cleanup: removed %d stale file(s)", removed)
            except Exception as exc:
                logger.debug("Image cache cleanup error: %s", exc)
            try:
                removed = cleanup_document_cache(max_age_hours=24)
                if removed:
                    logger.info("Document cache cleanup: removed %d stale file(s)", removed)
            except Exception as exc:
                logger.debug("Document cache cleanup error: %s", exc)

        if tick_count % paste_sweep_every == 0:
            try:
                deleted, remaining = _sweep_expired_pastes()
                if deleted:
                    logger.info(
                        "Paste sweep: deleted %d expired paste(s), %d pending",
                        deleted,
                        remaining,
                    )
            except Exception as exc:
                logger.debug("Paste sweep error: %s", exc)

        if tick_count % curator_every == 0:
            try:
                from agent.curator import maybe_run_curator

                maybe_run_curator(
                    idle_for_seconds=float("inf"),
                    on_summary=lambda msg: logger.info("curator: %s", msg),
                )
            except Exception as exc:
                logger.debug("Curator tick error: %s", exc)

        stop_event.wait(timeout=interval)
    logger.info("Cron ticker stopped")
