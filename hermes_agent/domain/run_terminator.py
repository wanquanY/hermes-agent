"""Phase C — RunStateMachine single-entry terminate_run (spec §7.2).

Atomic terminal transition:

    BEGIN IMMEDIATE
        SELECT status FROM runs WHERE run_id = ?
        if already terminal → IDEMPOTENT_SKIP
        allocate terminal_seq via SeqAllocator (UPDATE seq_counter RETURNING)
        INSERT terminal canonical event into run_events (session_id, seq)
        UPDATE runs SET status, terminal_seq, terminal_cause, completed_at
    COMMIT

On ``SeqAllocatorBusy`` (SQLite locked after 3 exponential backoffs) → DEGRADED:
    - runs.status is still updated (business must move forward)
    - runs.terminal_degraded = 1 flags the row for background reconciliation
    - the canonical terminal event is NOT appended in this transaction
    - the caller is expected to log ``event=degrade cause=seq_busy``

Cause priority — ``WORKER_CRASHED > MANUAL_KILL > WORKER_EMITTED`` (spec §7.3)
is enforced by "terminal not downgradable": once a terminal state is written,
any later terminate_run call returns IDEMPOTENT_SKIP without overwriting.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.exceptions import SeqAllocatorBusy
from hermes_agent.domain.run_state_machine import TERMINAL_RUN_STATUSES


_logger = logging.getLogger(__name__)


class TerminateCause(str, Enum):
    """The origin of a terminate_run call (spec §7.3)."""

    WORKER_EMITTED = "worker_emitted"
    WORKER_CRASHED = "worker_crashed"
    MANUAL_KILL = "manual_kill"
    SESSION_CLOSED = "session_closed"
    ERROR_PROPAGATION = "error_propagation"


class TerminateOutcome(str, Enum):
    APPLIED = "applied"                     # first terminal write; event + status
    IDEMPOTENT_SKIP = "idempotent_skip"     # run was already terminal
    DEGRADED = "degraded"                   # SeqAllocatorBusy; status-only update


@dataclass(frozen=True)
class TerminateResult:
    outcome: TerminateOutcome
    run_id: str
    session_id: str
    terminal_status: str                    # final observed / applied status
    terminal_seq: int                       # 0 for DEGRADED / IDEMPOTENT_SKIP without seq
    cause: TerminateCause
    degraded: bool                          # True when SeqAllocatorBusy took the escape hatch


# Mapping target_status → canonical event type + payload.status.
# spec §6.2 CanonicalEvent 14 arm: `error` for failed, `message.complete` for
# {completed, interrupted, cancelled}.
_TERMINAL_EVENT_BY_STATUS = {
    "completed":   ("message.complete", "completed"),
    "interrupted": ("message.complete", "interrupted"),
    "cancelled":   ("message.complete", "cancelled"),
    "failed":      ("error",            "failed"),
}


def terminate_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    session_id: str,
    target_status: str,
    cause: TerminateCause | str,
    turn_id: str = "",
    message: str = "",
    payload_extra: dict | None = None,
    now: float | None = None,
) -> TerminateResult:
    """Single entrypoint for terminal state transitions (spec §7.2).

    Idempotent: if the run is already terminal, no event or status change is
    written; the observed terminal state is returned via
    ``TerminateOutcome.IDEMPOTENT_SKIP``. Degrades gracefully on
    ``SeqAllocatorBusy`` — the ``runs.status`` update still commits.
    """

    normalized_run = str(run_id or "").strip()
    normalized_sid = str(session_id or "").strip()
    normalized_target = str(target_status or "").strip().lower()
    if not normalized_run or not normalized_sid:
        raise ValueError("run_id and session_id are required")
    if normalized_target not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"invalid terminal target: {target_status!r}")

    resolved_cause = cause if isinstance(cause, TerminateCause) else TerminateCause(str(cause))
    ts = float(now if now is not None else time.time())
    turn = str(turn_id or "").strip()

    event_type, payload_status = _TERMINAL_EVENT_BY_STATUS[normalized_target]
    payload: dict = {
        "run_id": normalized_run,
        "turn_id": turn,
        "status": payload_status,
    }
    if payload_extra:
        payload.update(payload_extra)
    if message:
        payload["message"] = str(message)
        if payload_status == "failed":
            payload["error_code"] = payload.get("error_code") or "runtime_error"

    owns_tx = not conn.in_transaction
    if owns_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT status, terminal_seq, terminal_degraded FROM runs WHERE run_id = ?",
            (normalized_run,),
        ).fetchone()
        existing_status = str((existing[0] if existing else "") or "").strip().lower()
        existing_terminal_seq = int((existing[1] if existing else 0) or 0)
        existing_degraded = bool(int((existing[2] if existing else 0) or 0))

        # Idempotency: terminal states are absorbing.
        if existing_status in TERMINAL_RUN_STATUSES:
            if owns_tx:
                conn.execute("COMMIT")
            _logger.debug(
                "terminate_run idempotent_skip run=%s existing=%s incoming=%s cause=%s",
                normalized_run,
                existing_status,
                normalized_target,
                resolved_cause.value,
            )
            return TerminateResult(
                outcome=TerminateOutcome.IDEMPOTENT_SKIP,
                run_id=normalized_run,
                session_id=normalized_sid,
                terminal_status=existing_status,
                terminal_seq=existing_terminal_seq,
                cause=resolved_cause,
                degraded=existing_degraded,
            )

        # Allocate terminal_seq atomically (still inside BEGIN IMMEDIATE).
        try:
            terminal_seq = _allocate_terminal_seq(conn, session_id=normalized_sid, now=ts)
        except SeqAllocatorBusy:
            # spec §6.3 degrade: update runs.status; skip event append; backend
            # reconciliation job resurrects the missing canonical event later.
            conn.execute(
                """
                UPDATE runs
                   SET status = ?,
                       terminal_degraded = 1,
                       terminal_cause = ?,
                       completed_at = ?,
                       updated_at = ?,
                       error = COALESCE(NULLIF(?, ''), error)
                 WHERE run_id = ?
                """,
                (
                    normalized_target,
                    resolved_cause.value,
                    ts,
                    ts,
                    str(message or ""),
                    normalized_run,
                ),
            )
            if owns_tx:
                conn.execute("COMMIT")
            _logger.warning(
                "event=degrade cause=seq_busy run=%s target=%s terminate_cause=%s",
                normalized_run,
                normalized_target,
                resolved_cause.value,
            )
            return TerminateResult(
                outcome=TerminateOutcome.DEGRADED,
                run_id=normalized_run,
                session_id=normalized_sid,
                terminal_status=normalized_target,
                terminal_seq=0,
                cause=resolved_cause,
                degraded=True,
            )

        # Canonical terminal event via EventLedger single entrypoint
        # (spec §6.1 — run_events is the one ledger). We pass the seq we
        # already allocated inside this same BEGIN IMMEDIATE transaction so
        # the ledger only performs its idempotency check + INSERT, without
        # touching seq_counter again.
        ledger = EventLedger(conn)
        ledger.append(
            session_id=normalized_sid,
            run_id=normalized_run,
            event_type=event_type,
            payload=payload,
            turn_id=turn,
            now=ts,
            preassigned_seq=terminal_seq,
        )

        conn.execute(
            """
            UPDATE runs
               SET status = ?,
                   terminal_seq = ?,
                   terminal_degraded = 0,
                   terminal_cause = ?,
                   completed_at = ?,
                   updated_at = ?,
                   last_seq = MAX(COALESCE(last_seq, 0), ?),
                   error = COALESCE(NULLIF(?, ''), error)
             WHERE run_id = ?
            """,
            (
                normalized_target,
                terminal_seq,
                resolved_cause.value,
                ts,
                ts,
                terminal_seq,
                str(message or ""),
                normalized_run,
            ),
        )
        if owns_tx:
            conn.execute("COMMIT")

        return TerminateResult(
            outcome=TerminateOutcome.APPLIED,
            run_id=normalized_run,
            session_id=normalized_sid,
            terminal_status=normalized_target,
            terminal_seq=terminal_seq,
            cause=resolved_cause,
            degraded=False,
        )
    except Exception:
        if not owns_tx:
            # Let the outer transaction manager decide; don't rollback theirs.
            raise
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as rollback_exc:
            # Rollback may itself fail (connection already broken); the outer
            # raise carries the true root cause. We record the rollback
            # failure so post-mortems can distinguish it from a clean abort.
            _logger.warning(
                "terminate_run rollback failed run=%s: %s", run_id, rollback_exc
            )
        raise


def _allocate_terminal_seq(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    now: float,
) -> int:
    """Allocate a monotonic seq via ``seq_counter`` inside the caller's tx.

    Raises ``SeqAllocatorBusy`` on repeated ``sqlite3.OperationalError`` — the
    outer transaction stays open so the caller can decide (DEGRADED path).
    """

    # Ensure a counter row exists for this session; INSERT OR IGNORE avoids
    # overwriting an existing counter that may already be ahead of MAX(seq).
    conn.execute(
        """
        INSERT OR IGNORE INTO seq_counter (session_id, next_seq, updated_at)
        SELECT ?, COALESCE(MAX(seq), 0) + 1, ?
          FROM run_events
         WHERE session_id = ?
        """,
        (session_id, float(now or 0), session_id),
    )
    # No row means the session_id row is fully missing (empty run_events too);
    # fall back to a bare counter row.
    conn.execute(
        """
        INSERT OR IGNORE INTO seq_counter (session_id, next_seq, updated_at)
        VALUES (?, 1, ?)
        """,
        (session_id, float(now or 0)),
    )
    row = conn.execute(
        """
        UPDATE seq_counter
           SET next_seq = next_seq + 1,
               updated_at = ?
         WHERE session_id = ?
        RETURNING next_seq - 1
        """,
        (float(now or 0), session_id),
    ).fetchone()
    if row is None:
        raise SeqAllocatorBusy(f"seq_counter unavailable for session {session_id}")
    return int(row[0])
