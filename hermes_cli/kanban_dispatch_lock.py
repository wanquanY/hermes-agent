"""Board-scoped single-writer lease for Kanban dispatcher ticks."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from hermes_agent.storage.process_lock import (
    ProcessLockError,
    exclusive_process_lock,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchLockDecision:
    acquired: bool
    path: Path
    reason: str | None = None


@contextlib.contextmanager
def dispatch_tick_lock(db_path: Path) -> Iterator[DispatchLockDecision]:
    """Try to own one board tick without blocking a gateway event loop.

    Acquisition/open errors are returned as an unacquired decision so callers
    skip without side effects. Release errors propagate because the tick may
    already have executed and must not be misreported as simple contention.
    """

    lock_path = Path(db_path).with_name(Path(db_path).name + ".dispatch.lock")
    manager = exclusive_process_lock(lock_path, blocking=False)
    try:
        lease = manager.__enter__()
    except ProcessLockError as exc:
        reason = f"process_lock_error:{exc}"
        logger.error("kanban dispatcher lock failed closed: %s", reason)
        yield DispatchLockDecision(False, lock_path, reason)
        return

    try:
        if not lease.acquired:
            yield DispatchLockDecision(False, lock_path, "contended")
        else:
            yield DispatchLockDecision(True, lock_path)
    finally:
        manager.__exit__(None, None, None)
