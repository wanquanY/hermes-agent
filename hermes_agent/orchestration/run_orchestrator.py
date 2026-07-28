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
import threading
import time
from dataclasses import dataclass

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_identity import RunIdentity
from hermes_agent.domain.run_state_machine import ACTIVE_RUN_STATUSES
from hermes_agent.domain.run_terminator import (
    TerminateCause,
    TerminateResult,
    terminate_run as _terminate_run_atomic,
)
from hermes_agent.orchestration.worker_pool import InflightRun, WorkerPool
from hermes_agent.repositories.run_repo import RunRepoImpl


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunLaunchSpec:
    run_id: str
    session_id: str
    worker_id: str
    turn_id: str = ""
    runtime_scope_key: str = ""
    agent_profile_id: str = ""


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
        self._launch_lock = threading.RLock()

    # ------------------------------------------------------------------

    def launch(
        self,
        conn: sqlite3.Connection,
        spec: RunLaunchSpec,
        *,
        now: float | None = None,
    ) -> RunLaunchResult:
        incoming = RunIdentity.create(
            run_id=spec.run_id,
            session_id=spec.session_id,
            worker_id=spec.worker_id,
            runtime_scope_key=spec.runtime_scope_key,
            agent_profile_id=spec.agent_profile_id,
            require_worker=True,
        )

        # Launch is a single identity decision across both durable and in-memory
        # owners. The lock also makes one sqlite connection safe from concurrent
        # launch attempts in callers that have not installed a sharded RPC lock.
        with self._launch_lock:
            self._pool.validate_run_start(
                worker_id=incoming.worker_id,
                run_id=incoming.run_id,
                session_id=incoming.session_id,
                runtime_scope_key=incoming.runtime_scope_key,
                agent_profile_id=incoming.agent_profile_id,
            )
            start_seq = self._claim_and_append_start(conn, incoming, spec, now=now)
            inflight = self._pool.record_run_start(
                worker_id=incoming.worker_id,
                run_id=incoming.run_id,
                session_id=incoming.session_id,
                turn_id=str(spec.turn_id or ""),
                runtime_scope_key=incoming.runtime_scope_key,
                agent_profile_id=incoming.agent_profile_id,
                now=now,
            )
            return RunLaunchResult(inflight=inflight, start_seq=start_seq)

    def _claim_and_append_start(
        self,
        conn: sqlite3.Connection,
        incoming: RunIdentity,
        spec: RunLaunchSpec,
        *,
        now: float | None,
    ) -> int:
        owns_tx = not conn.in_transaction
        savepoint = "hermes_run_identity_launch"
        if owns_tx:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"SAVEPOINT {savepoint}")
        try:
            claimed = RunRepoImpl(conn).claim_identity(incoming)

            prior = conn.execute(
                """
                SELECT seq
                  FROM run_events
                 WHERE session_id = ? AND run_id = ? AND event_type = ?
                 ORDER BY seq ASC
                 LIMIT 1
                """,
                (
                    incoming.session_id,
                    incoming.run_id,
                    self.RUN_STARTED_EVENT_TYPE,
                ),
            ).fetchone()
            if prior is None:
                outcome = EventLedger(conn).append(
                    session_id=incoming.session_id,
                    run_id=incoming.run_id,
                    event_type=self.RUN_STARTED_EVENT_TYPE,
                    payload={
                        "run_id": incoming.run_id,
                        "worker_id": claimed.worker_id,
                        "turn_id": str(spec.turn_id or ""),
                        "runtime_scope_key": claimed.runtime_scope_key,
                        "agent_profile_id": claimed.agent_profile_id,
                    },
                    turn_id=str(spec.turn_id or ""),
                    now=now,
                )
                start_seq = outcome.seq
            else:
                start_seq = int(self._row_value(prior, "seq", 0) or 0)

            if owns_tx:
                conn.execute("COMMIT")
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return start_seq
        except Exception:
            if owns_tx:
                conn.execute("ROLLBACK")
            else:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

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
                   worker_id, agent_profile_id, status
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
            worker_id = self._row_value(row, "worker_id", 4) or "unknown"
            self._pool.record_run_start(
                worker_id=worker_id,
                run_id=run_id,
                session_id=session_id,
                turn_id=self._row_value(row, "turn_id", 3) or "",
                runtime_scope_key=self._row_value(row, "runtime_scope_key", 2) or "",
                agent_profile_id=self._row_value(row, "agent_profile_id", 5) or "",
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
