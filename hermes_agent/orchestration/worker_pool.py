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
import threading
from typing import Iterable

from hermes_agent.domain.run_identity import (
    RunIdentity,
    ensure_run_identity_compatible,
)


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InflightRun:
    run_id: str
    session_id: str
    worker_id: str
    allocated_at: float
    turn_id: str = ""
    runtime_scope_key: str = ""
    agent_profile_id: str = ""

    @property
    def identity(self) -> RunIdentity:
        return RunIdentity.create(
            run_id=self.run_id,
            session_id=self.session_id,
            worker_id=self.worker_id,
            runtime_scope_key=self.runtime_scope_key,
            agent_profile_id=self.agent_profile_id,
            require_worker=True,
        )


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
        self._lock = threading.RLock()

    def validate_run_start(
        self,
        *,
        worker_id: str,
        run_id: str,
        session_id: str,
        runtime_scope_key: str = "",
        agent_profile_id: str = "",
    ) -> InflightRun | None:
        incoming = RunIdentity.create(
            run_id=run_id,
            session_id=session_id,
            worker_id=worker_id,
            runtime_scope_key=runtime_scope_key,
            agent_profile_id=agent_profile_id,
            require_worker=True,
        )
        with self._lock:
            existing = self._inflight_by_run_id.get(incoming.run_id)
            if existing is not None:
                ensure_run_identity_compatible(existing.identity, incoming)
            return existing

    def record_run_start(
        self,
        *,
        worker_id: str,
        run_id: str,
        session_id: str,
        turn_id: str = "",
        runtime_scope_key: str = "",
        agent_profile_id: str = "",
        now: float | None = None,
    ) -> InflightRun:
        incoming = RunIdentity.create(
            run_id=run_id,
            session_id=session_id,
            worker_id=worker_id,
            runtime_scope_key=runtime_scope_key,
            agent_profile_id=agent_profile_id,
            require_worker=True,
        )
        with self._lock:
            existing = self._inflight_by_run_id.get(incoming.run_id)
            if existing is not None:
                claimed = existing.identity.claimed_with(incoming)
                if claimed == existing.identity:
                    return existing
                record = InflightRun(
                    run_id=claimed.run_id,
                    session_id=claimed.session_id,
                    worker_id=claimed.worker_id,
                    allocated_at=existing.allocated_at,
                    turn_id=existing.turn_id,
                    runtime_scope_key=claimed.runtime_scope_key,
                    agent_profile_id=claimed.agent_profile_id,
                )
                self._inflight_by_run_id[claimed.run_id] = record
                return record
            record = InflightRun(
                run_id=incoming.run_id,
                session_id=incoming.session_id,
                worker_id=incoming.worker_id,
                allocated_at=float(now if now is not None else time.time()),
                turn_id=str(turn_id or ""),
                runtime_scope_key=incoming.runtime_scope_key,
                agent_profile_id=incoming.agent_profile_id,
            )
            self._inflight_by_run_id[incoming.run_id] = record
            self._runs_by_worker.setdefault(incoming.worker_id, set()).add(
                incoming.run_id
            )
            return record

    def record_run_terminal(self, run_id: str) -> InflightRun | None:
        r = str(run_id or "").strip()
        with self._lock:
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
        with self._lock:
            return self._inflight_by_run_id.get(str(run_id or "").strip())

    def runs_for_worker(self, worker_id: str) -> list[InflightRun]:
        w = str(worker_id or "").strip()
        with self._lock:
            ids = self._runs_by_worker.get(w, set())
            return [self._inflight_by_run_id[r] for r in sorted(ids)]

    def workers(self) -> list[str]:
        with self._lock:
            return sorted(self._runs_by_worker.keys())

    def size(self) -> int:
        with self._lock:
            return len(self._inflight_by_run_id)

    def inflight(self) -> Iterable[InflightRun]:
        with self._lock:
            return list(self._inflight_by_run_id.values())
