"""ADR-0001 §Phase 1.C reconciler service registration.

Registers ActivityReconciler per session store and exposes startup-time hook.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from tui_gateway.services.activity_reconciler import (
    ActivityReconciler,
    reconciler_disabled,
)

logger = logging.getLogger(__name__)

_reconcilers: dict[int, ActivityReconciler] = {}
_lock = threading.Lock()


def register_session_db_for_reconciliation(
    db: Any,
    *,
    logger_: logging.Logger | None = None,
) -> ActivityReconciler:
    """Idempotent per-db registration. Returns the reconciler."""
    if reconciler_disabled():
        return ActivityReconciler(db, logger_=logger_)
    key = id(db)
    with _lock:
        existing = _reconcilers.get(key)
        if existing is not None:
            return existing
        reconciler = ActivityReconciler(db, logger_=logger_)
        _reconcilers[key] = reconciler
        return reconciler


def stop_all_reconcilers(*, timeout: float = 5.0) -> None:
    """Used by tests / shutdown to drain reconcilers."""
    with _lock:
        items = list(_reconcilers.values())
        _reconcilers.clear()
    for reconciler in items:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(reconciler.stop(timeout=timeout))
        finally:
            loop.close()
