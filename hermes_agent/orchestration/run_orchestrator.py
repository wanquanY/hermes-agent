"""RunOrchestrator — L3 spawn / dispatch / terminate coordinator (spec §3).

Composes three L2 primitives:

* ``WorkerPool``  — single-replica in-flight run state (spec §8.1)
* ``EventLedger`` — the one canonical event ledger (spec §6.1)
* ``run_terminator.terminate_run`` — the sole terminal transition (spec §7.2)

The orchestrator does not open its own transaction — callers supply the
``sqlite3.Connection``. ``WorkerPool`` state stays in-memory alongside the
``EventLedger`` writes; a crash between the two leaves ``run_events`` as the
authoritative source and ``WorkerPool`` gets rebuilt on restart from
``runs WHERE status IN ACTIVE_RUN_STATUSES``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_state_machine import ACTIVE_RUN_STATUSES
from hermes_agent.domain.run_terminator import (
    TerminateCause,
    TerminateResult,
    terminate_run as _terminate_run_atomic,
)
from hermes_agent.orchestration.worker_pool import InflightRun, WorkerPool


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunLaunchSpec:
    run_id: str
    session_id: str
    worker_id: str
    turn_id: str = ""
    runtime_scope_key: str = ""


@dataclass(frozen=True)
class RunLaunchResult:
    inflight: InflightRun
    start_seq: int


class RunOrchestrator:
    """Coordinates run lifecycle over WorkerPool + EventLedger + terminator.

    Interface:

        orch.launch(conn, spec) -> RunLaunchResult
            * appends ``run.started`` canonical event
            * records inflight in WorkerPool

        orch.terminate(conn, run_id, session_id, target_status, cause)
            * delegates to the domain terminate_run
            * clears the inflight entry on APPLIED or DEGRADED
            * IDEMPOTENT_SKIP does NOT touch WorkerPool (already reaped)

        orch.reap_orphans(conn) -> list[str]
            * rebuilds WorkerPool from ``runs`` rows with active status
            * called on gateway restart to recover from crashes
    """

    RUN_STARTED_EVENT_TYPE = "run.started"

    def __init__(self, pool: WorkerPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------

    def launch(
        self,
        conn: sqlite3.Connection,
        spec: RunLaunchSpec,
        *,
        now: float | None = None,
    ) -> RunLaunchResult:
        stable_run = str(spec.run_id or "").strip()
        stable_session = str(spec.session_id or "").strip()
        stable_worker = str(spec.worker_id or "").strip()
        if not stable_run or not stable_session or not stable_worker:
            raise ValueError("run_id, session_id, worker_id are required")

        ledger = EventLedger(conn)
        outcome = ledger.append(
            session_id=stable_session,
            run_id=stable_run,
            event_type=self.RUN_STARTED_EVENT_TYPE,
            payload={
                "run_id": stable_run,
                "worker_id": stable_worker,
                "turn_id": str(spec.turn_id or ""),
                "runtime_scope_key": str(spec.runtime_scope_key or ""),
            },
            turn_id=str(spec.turn_id or ""),
            now=now,
        )
        inflight = self._pool.record_run_start(
            worker_id=stable_worker,
            run_id=stable_run,
            session_id=stable_session,
            turn_id=str(spec.turn_id or ""),
            runtime_scope_key=str(spec.runtime_scope_key or ""),
            now=now,
        )
        return RunLaunchResult(inflight=inflight, start_seq=outcome.seq)

    def terminate(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        session_id: str,
        target_status: str,
        cause: TerminateCause | str = TerminateCause.WORKER_EMITTED,
        turn_id: str = "",
        message: str = "",
    ) -> TerminateResult:
        result = _terminate_run_atomic(
            conn,
            run_id=run_id,
            session_id=session_id,
            target_status=target_status,
            cause=cause,
            turn_id=turn_id,
            message=message,
        )
        # Clear inflight state on any real terminal transition. Idempotent
        # skip means the pool already lost the entry (or another path did).
        if result.outcome.value in {"applied", "degraded"}:
            self._pool.record_run_terminal(run_id)
        return result

    def reap_orphans(self, conn: sqlite3.Connection) -> list[str]:
        """Rebuild WorkerPool from active runs after a gateway restart.

        Returns the list of run_ids that were re-attached. ``worker_id`` is
        derived from the persisted ``runs`` row where available; when the
        column is empty, ``"unknown"`` is used so we still track the run.
        """
        rows = conn.execute(
            """
            SELECT run_id, session_id, runtime_scope_key, turn_id,
                   runtime_session_id, status
              FROM runs
             WHERE status IN ({placeholders})
            """.format(
                placeholders=",".join(["?"] * len(ACTIVE_RUN_STATUSES)),
            ),
            tuple(sorted(ACTIVE_RUN_STATUSES)),
        ).fetchall()
        recovered: list[str] = []
        now = time.time()
        for row in rows:
            run_id = self._row_value(row, "run_id", 0)
            session_id = self._row_value(row, "session_id", 1)
            if not run_id or not session_id:
                continue
            worker_id = self._row_value(row, "runtime_scope_key", 2) or "unknown"
            self._pool.record_run_start(
                worker_id=worker_id,
                run_id=run_id,
                session_id=session_id,
                turn_id=self._row_value(row, "turn_id", 3) or "",
                runtime_scope_key=self._row_value(row, "runtime_scope_key", 2) or "",
                now=now,
            )
            recovered.append(run_id)
        return recovered

    @property
    def pool(self) -> WorkerPool:
        return self._pool

    # ------------------------------------------------------------------

    @staticmethod
    def _row_value(row, key: str, idx: int) -> str:
        if isinstance(row, sqlite3.Row):
            value = row[key]
        else:
            try:
                value = row[idx]
            except (IndexError, KeyError):
                value = None
        return str(value or "").strip()
