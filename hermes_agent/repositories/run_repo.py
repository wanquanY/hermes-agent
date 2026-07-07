"""RunRepo protocol + concrete impl (spec §4.2) — runs + run_events."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from hermes_agent.domain.canonical_event import CanonicalEvent as DomainCanonicalEvent
from hermes_agent.domain.event_ledger import EventLedger, LedgerEvent
from hermes_agent.domain.run_terminator import (
    TerminateCause,
    TerminateResult,
    terminate_run as _terminate_run_atomic,
)
from hermes_agent.repositories.base import RepositoryConnection


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    session_id: str
    turn_id: str = ""
    runtime_scope_key: str = ""
    runtime_session_id: str = ""
    status: str = "running"


@dataclass(frozen=True)
class Run:
    run_id: str
    session_id: str
    status: str
    started_at: float
    updated_at: float
    completed_at: float | None = None
    turn_id: str = ""
    runtime_scope_key: str = ""
    runtime_session_id: str = ""
    last_seq: int = 0
    terminal_seq: int = 0
    terminal_degraded: bool = False
    terminal_cause: str = ""


@dataclass(frozen=True)
class CanonicalEventSpec:
    """L1 persistence-layer append parameter for ``RunRepo.append_event``.

    Distinct from ``hermes_agent.domain.CanonicalEvent`` (spec §6.2 line
    412-420) — that one is the **typed wire event** with
    ``payload: TypedPayload``. This dataclass is the **serialized form**
    heading into the SQLite ``run_events.payload_json`` column, so
    ``payload`` is intentionally a plain dict (whatever the caller
    produced by ``dataclasses.asdict()`` on the typed payload).

    Use ``from_canonical(event)`` to convert from the typed form when
    the caller is holding a domain ``CanonicalEvent``.

    Audit note (docs/v3_audit_report.md §五 #1) confirmed this is not a
    spec §6.2 violation once distinguished from ``CanonicalEvent``.
    """

    event_type: str
    payload: dict[str, Any]
    run_id: str
    turn_id: str = ""
    preassigned_seq: int | None = None

    @classmethod
    def from_canonical(cls, event: "DomainCanonicalEvent") -> "CanonicalEventSpec":
        """Build a persistence spec from a typed domain ``CanonicalEvent``."""
        from dataclasses import asdict

        return cls(
            event_type=event.type.value,
            payload=asdict(event.payload),
            run_id=event.run_id,
            turn_id=event.turn_id or "",
            preassigned_seq=event.seq,
        )


@runtime_checkable
class RunRepo(Protocol):
    def create_run(self, session_id: str, spec: RunSpec) -> Run: ...

    def get_run(self, run_id: str) -> Run | None: ...

    def append_event(
        self,
        session_id: str,
        event: CanonicalEventSpec,
        *,
        allocate_seq: bool = True,
    ) -> int: ...

    def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int = 0,
        types: set[str] | None = None,
        include_internal: bool = False,
        limit: int = 200,
    ) -> list[LedgerEvent]: ...

    def list_tool_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[LedgerEvent]: ...

    def set_terminal(
        self,
        run_id: str,
        session_id: str,
        target_status: str,
        cause: TerminateCause | str = TerminateCause.WORKER_EMITTED,
    ) -> TerminateResult: ...


class RunRepoImpl:
    """SQLite-backed RunRepo (spec §4.2).

    * ``append_event`` and ``list_events`` delegate to ``EventLedger`` so
      run_events is the one ledger (spec §6.1).
    * ``set_terminal`` delegates to ``domain.run_terminator.terminate_run`` —
      the ONLY authorised terminal transition (spec §7.2).
    """

    _TOOL_EVENT_TYPES = frozenset({
        "tool.start",
        "tool.generating",
        "tool.progress",
        "tool.delta",
        "tool.complete",
    })

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn
        self._ledger = EventLedger(conn)

    # ------------------------------------------------------------------

    def create_run(self, session_id: str, spec: RunSpec) -> Run:
        stable_sid = str(session_id or "").strip()
        stable_run = str(spec.run_id or "").strip()
        if not stable_sid or not stable_run:
            raise ValueError("session_id and RunSpec.run_id are required")
        now = time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, session_id, runtime_scope_key, turn_id,
                runtime_session_id, status, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_run,
                stable_sid,
                str(spec.runtime_scope_key or ""),
                str(spec.turn_id or ""),
                str(spec.runtime_session_id or ""),
                str(spec.status or "running"),
                now,
                now,
            ),
        )
        got = self.get_run(stable_run)
        assert got is not None
        return got

    def get_run(self, run_id: str) -> Run | None:
        stable = str(run_id or "").strip()
        if not stable:
            return None
        row = self._conn.execute(
            """
            SELECT run_id, session_id, status, started_at, updated_at,
                   completed_at, turn_id, runtime_scope_key, runtime_session_id,
                   last_seq, terminal_seq, terminal_degraded, terminal_cause
              FROM runs
             WHERE run_id = ?
            """,
            (stable,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_run(row)

    def append_event(
        self,
        session_id: str,
        event: CanonicalEventSpec,
        *,
        allocate_seq: bool = True,
    ) -> int:
        preassigned = None if allocate_seq else event.preassigned_seq
        outcome = self._ledger.append(
            session_id=session_id,
            run_id=event.run_id,
            event_type=event.event_type,
            payload=event.payload,
            turn_id=event.turn_id,
            preassigned_seq=preassigned,
        )
        return outcome.seq

    def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int = 0,
        types: set[str] | None = None,
        include_internal: bool = False,
        limit: int = 200,
    ) -> list[LedgerEvent]:
        return self._ledger.list(
            session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            types=types,
            include_internal=include_internal,
            limit=limit,
        )

    def list_tool_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[LedgerEvent]:
        """Tool events are a projection over run_events (spec §6.1)."""
        return self._ledger.list(
            session_id,
            after_seq=after_seq,
            types=set(self._TOOL_EVENT_TYPES),
            include_internal=False,
            limit=limit,
        )

    def set_terminal(
        self,
        run_id: str,
        session_id: str,
        target_status: str,
        cause: TerminateCause | str = TerminateCause.WORKER_EMITTED,
    ) -> TerminateResult:
        return _terminate_run_atomic(
            self._conn,
            run_id=run_id,
            session_id=session_id,
            target_status=target_status,
            cause=cause,
        )


def _row_to_run(row: Any) -> Run:
    def _g(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    completed_at_raw = _g("completed_at", 5)
    return Run(
        run_id=str(_g("run_id", 0) or ""),
        session_id=str(_g("session_id", 1) or ""),
        status=str(_g("status", 2) or ""),
        started_at=float(_g("started_at", 3) or 0),
        updated_at=float(_g("updated_at", 4) or 0),
        completed_at=float(completed_at_raw) if completed_at_raw is not None else None,
        turn_id=str(_g("turn_id", 6) or ""),
        runtime_scope_key=str(_g("runtime_scope_key", 7) or ""),
        runtime_session_id=str(_g("runtime_session_id", 8) or ""),
        last_seq=int(_g("last_seq", 9) or 0),
        terminal_seq=int(_g("terminal_seq", 10) or 0),
        terminal_degraded=bool(int(_g("terminal_degraded", 11) or 0)),
        terminal_cause=str(_g("terminal_cause", 12) or ""),
    )


__all__ = [
    "CanonicalEventSpec",
    "Run",
    "RunRepo",
    "RunRepoImpl",
    "RunSpec",
]
