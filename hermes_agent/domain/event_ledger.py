"""EventLedger — the single canonical event ledger (spec §6.1, §6.4).

Wraps ``run_events`` reads/writes behind an append-only, monotonically
seq-ordered contract. This is the L2 domain service the L3 orchestration
and L4 gateway layers should use to append or replay events.

Key contracts (spec §6.4):
* ``append`` is idempotent under ``(session_id, seq)``
* ``list`` returns events in strict seq order per session_id
* internal ``_internal.*`` events are filtered by default so downstream
  consumers never see interaction persistence artefacts (spec §7.4)

The tool_events read model becomes a materialized view maintained by
triggers (Phase E migration 0046) — its rows are derived from run_events
and this ledger is authoritative.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from hermes_agent.domain.exceptions import SeqAllocatorBusy


_logger = logging.getLogger(__name__)


INTERNAL_EVENT_PREFIX = "_internal."


class AppendResult(str, Enum):
    APPLIED = "applied"
    IDEMPOTENT_SKIP = "idempotent_skip"


@dataclass(frozen=True)
class LedgerAppendOutcome:
    result: AppendResult
    seq: int              # allocated seq; 0 on IDEMPOTENT_SKIP
    session_id: str
    event_type: str


@dataclass(frozen=True)
class LedgerEvent:
    """Envelope surfaced by ``EventLedger.list``.

    ``payload`` is the decoded JSON — payload_json is the wire form.
    """

    session_id: str
    run_id: str
    seq: int
    event_type: str
    turn_id: str
    timestamp: float
    payload: dict[str, Any]


class EventLedger:
    """Session-scoped ledger façade around ``run_events``.

    Currently backed by SQLite via ``sqlite3.Connection``. Later phases can
    substitute a repository-backed impl without changing this API.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(
        self,
        *,
        session_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        turn_id: str = "",
        now: float | None = None,
        preassigned_seq: int | None = None,
    ) -> LedgerAppendOutcome:
        """Append a canonical event.

        ``preassigned_seq`` supports callers (RunStateMachine) that already
        drew a seq inside their own transaction. When omitted, the ledger
        transactionally allocates one via ``seq_counter``.
        """

        stable_sid = str(session_id or "").strip()
        stable_run = str(run_id or "").strip()
        etype = str(event_type or "").strip()
        if not stable_sid or not etype:
            raise ValueError("session_id and event_type are required")

        ts = float(now if now is not None else time.time())
        payload_json = _dumps(payload or {})
        envelope = {
            "type": etype,
            "session_id": stable_sid,
            "run_id": stable_run,
            "turn_id": turn_id,
            "seq": preassigned_seq or 0,  # patched after allocation
            "timestamp": ts,
            "payload": payload or {},
        }

        if preassigned_seq is not None:
            seq = int(preassigned_seq)
            envelope["seq"] = seq
            event_json = _dumps(envelope)
            row = self._conn.execute(
                "SELECT 1 FROM run_events WHERE session_id = ? AND seq = ?",
                (stable_sid, seq),
            ).fetchone()
            if row is not None:
                return LedgerAppendOutcome(
                    result=AppendResult.IDEMPOTENT_SKIP,
                    seq=seq,
                    session_id=stable_sid,
                    event_type=etype,
                )
            self._insert(
                stable_sid,
                stable_run,
                seq,
                etype,
                turn_id,
                ts,
                payload_json,
                event_json,
            )
            return LedgerAppendOutcome(
                result=AppendResult.APPLIED,
                seq=seq,
                session_id=stable_sid,
                event_type=etype,
            )

        # Allocate + append. When we are the outermost tx layer, open our own
        # BEGIN IMMEDIATE; otherwise piggy-back on the caller's transaction
        # (spec §6.4 — the ledger is composable inside RunStateMachine).
        owns_tx = not self._conn.in_transaction
        if owns_tx:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            seq = self._allocate_seq(stable_sid, ts)
            envelope["seq"] = seq
            event_json = _dumps(envelope)
            self._insert(
                stable_sid,
                stable_run,
                seq,
                etype,
                turn_id,
                ts,
                payload_json,
                event_json,
            )
            if owns_tx:
                self._conn.execute("COMMIT")
            return LedgerAppendOutcome(
                result=AppendResult.APPLIED,
                seq=seq,
                session_id=stable_sid,
                event_type=etype,
            )
        except Exception:
            if owns_tx:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError as rb_exc:
                    _logger.warning("EventLedger.append rollback failed: %s", rb_exc)
            raise

    def list(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int = 0,
        types: set[str] | None = None,
        include_internal: bool = False,
        limit: int = 200,
    ) -> list[LedgerEvent]:
        """List events with cursor + type filter.

        - ``include_internal=False`` (default) filters out ``_internal.*``
          prefixed events. spec §7.4 keeps ``_internal.interaction.*`` on
          disk for crash recovery but must not leak them to renderers.
        """
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return []
        clauses = ["session_id = ?"]
        params: list[Any] = [stable_sid]
        if after_seq:
            clauses.append("seq > ?")
            params.append(int(after_seq))
        if before_seq:
            clauses.append("seq < ?")
            params.append(int(before_seq))
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(sorted(types))
        if not include_internal:
            clauses.append("event_type NOT LIKE '_internal.%'")
        sql = (
            "SELECT session_id, run_id, seq, event_type, turn_id, timestamp, payload_json "
            "FROM run_events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY seq ASC "
            "LIMIT ?"
        )
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()

        return [_row_to_event(row) for row in rows]

    # ------------------------------------------------------------------

    def _allocate_seq(self, session_id: str, now: float) -> int:
        try:
            # Ensure seq_counter row exists (bootstrap from run_events MAX).
            self._conn.execute(
                """
                INSERT OR IGNORE INTO seq_counter (session_id, next_seq, updated_at)
                SELECT ?, COALESCE(MAX(seq), 0) + 1, ?
                  FROM run_events
                 WHERE session_id = ?
                """,
                (session_id, now, session_id),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO seq_counter (session_id, next_seq, updated_at)
                VALUES (?, 1, ?)
                """,
                (session_id, now),
            )
            row = self._conn.execute(
                """
                UPDATE seq_counter
                   SET next_seq = next_seq + 1,
                       updated_at = ?
                 WHERE session_id = ?
                RETURNING next_seq - 1
                """,
                (now, session_id),
            ).fetchone()
            if row is None:
                raise SeqAllocatorBusy(
                    f"seq_counter unavailable for session {session_id}"
                )
            return int(row[0])
        except sqlite3.OperationalError as exc:
            raise SeqAllocatorBusy(str(exc)) from exc

    def _insert(
        self,
        session_id: str,
        run_id: str,
        seq: int,
        event_type: str,
        turn_id: str,
        ts: float,
        payload_json: str,
        event_json: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO run_events (
                session_id, run_id, seq, event_type, turn_id, timestamp, payload_json, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, run_id, seq, event_type, turn_id, ts, payload_json, event_json),
        )


def _row_to_event(row: Any) -> LedgerEvent:
    if isinstance(row, sqlite3.Row):
        session_id = str(row["session_id"] or "")
        run_id = str(row["run_id"] or "")
        seq = int(row["seq"] or 0)
        etype = str(row["event_type"] or "")
        turn_id = str(row["turn_id"] or "")
        ts = float(row["timestamp"] or 0)
        payload_json = row["payload_json"]
    else:
        row_seq = list(row)
        session_id = str(row_seq[0] or "")
        run_id = str(row_seq[1] or "")
        seq = int(row_seq[2] or 0)
        etype = str(row_seq[3] or "")
        turn_id = str(row_seq[4] or "")
        ts = float(row_seq[5] or 0)
        payload_json = row_seq[6]
    try:
        payload = json.loads(payload_json) if payload_json else {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
    except json.JSONDecodeError:
        payload = {"_raw": payload_json}
    return LedgerEvent(
        session_id=session_id,
        run_id=run_id,
        seq=seq,
        event_type=etype,
        turn_id=turn_id,
        timestamp=ts,
        payload=payload,
    )


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
