"""WorkerPool — single-replica in-flight run state (spec §8.1).

Legacy design kept two copies of in-flight run identity:

* ``_LeaseState.inflight`` in the pool (spec §2.2 line 62)
* ``RunWorker.active_runs`` set on each worker (spec §2.2 line 62)

They drifted whenever a worker crashed between recording and reaping.
The pool becomes the single SSoT here: ``_inflight_by_run_id`` maps
``run_id → InflightRun`` and workers query the pool rather than owning
their own set.

Workers still process events; the pool answers "which runs is worker X
handling" by reverse lookup, so terminal detection stays cheap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InflightRun:
    run_id: str
    session_id: str
    worker_id: str
    allocated_at: float
    turn_id: str = ""
    runtime_scope_key: str = ""


class WorkerPool:
    """Single-replica in-flight run tracker.

    Interface (spec §8.1):

        pool.record_run_start(worker_id, run_id, session_id, ...)
        pool.record_run_terminal(run_id)
        pool.runs_for_worker(worker_id) -> list[InflightRun]
        pool.get(run_id) -> InflightRun | None
        pool.size() -> int

    Concurrency: caller-supplied. ``ShardedRpcLock`` typically wraps writes.
    """

    def __init__(self) -> None:
        self._inflight_by_run_id: dict[str, InflightRun] = {}
        self._runs_by_worker: dict[str, set[str]] = {}

    def record_run_start(
        self,
        *,
        worker_id: str,
        run_id: str,
        session_id: str,
        turn_id: str = "",
        runtime_scope_key: str = "",
        now: float | None = None,
    ) -> InflightRun:
        w = str(worker_id or "").strip()
        r = str(run_id or "").strip()
        s = str(session_id or "").strip()
        if not w or not r or not s:
            raise ValueError("worker_id, run_id, session_id are required")
        allocated_at = float(now if now is not None else time.time())
        record = InflightRun(
            run_id=r,
            session_id=s,
            worker_id=w,
            allocated_at=allocated_at,
            turn_id=str(turn_id or ""),
            runtime_scope_key=str(runtime_scope_key or ""),
        )
        existing = self._inflight_by_run_id.get(r)
        if existing is not None and existing.worker_id != w:
            # Existing worker is being replaced (e.g. respawn); reap the old
            # bucket so runs_for_worker stays accurate.
            self._runs_by_worker.get(existing.worker_id, set()).discard(r)
        self._inflight_by_run_id[r] = record
        self._runs_by_worker.setdefault(w, set()).add(r)
        return record

    def record_run_terminal(self, run_id: str) -> InflightRun | None:
        r = str(run_id or "").strip()
        record = self._inflight_by_run_id.pop(r, None)
        if record is None:
            return None
        bucket = self._runs_by_worker.get(record.worker_id)
        if bucket is not None:
            bucket.discard(r)
            if not bucket:
                del self._runs_by_worker[record.worker_id]
        return record

    def get(self, run_id: str) -> InflightRun | None:
        return self._inflight_by_run_id.get(str(run_id or "").strip())

    def runs_for_worker(self, worker_id: str) -> list[InflightRun]:
        w = str(worker_id or "").strip()
        ids = self._runs_by_worker.get(w, set())
        return [self._inflight_by_run_id[r] for r in sorted(ids)]

    def workers(self) -> list[str]:
        return sorted(self._runs_by_worker.keys())

    def size(self) -> int:
        return len(self._inflight_by_run_id)

    def inflight(self) -> Iterable[InflightRun]:
        return list(self._inflight_by_run_id.values())
