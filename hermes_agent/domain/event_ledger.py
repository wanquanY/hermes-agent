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
from hermes_agent.domain.interaction import InternalRunEventType
from hermes_agent.domain.seq_allocator import allocate_only


_logger = logging.getLogger(__name__)


INTERNAL_EVENT_PREFIX = "_internal."


def _normalized_text_values(values: Iterable[str]) -> list[str]:
    return list(
        dict.fromkeys(
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        )
    )


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
        activity_id: str = "",
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
                activity_id,
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
                activity_id,
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

    def append_runtime_frame(
        self,
        *,
        session_id: str,
        run_id: str,
        turn_id: str,
        execution_session_id: str,
        runtime_scope_key: str,
        participant_id: str,
        activity_id: str | None,
        event_type: str,
        seq: int,
        timestamp: float,
        payload_json: str,
        event_json: str,
        status: str,
        frame_blob: bytes | None,
        frame_format: str,
        retention_class: str,
        interaction_request_id: str | None = None,
        interaction_kind: str | None = None,
        interaction_status: str | None = None,
        anchor_seq: int = 0,
        projection_state: str = "raw",
        runtime_source_seq: int = 0,
    ) -> None:
        """Append a fully materialized runtime frame row.

        The gateway owns frame normalization and projections, while this domain
        service owns the physical ``run_events`` INSERT.
        """

        self._conn.execute(
            """
            INSERT OR IGNORE INTO run_events (
                session_id, run_id, turn_id, execution_session_id, runtime_scope_key,
                participant_id, activity_id, event_type,
                seq, timestamp, payload_json, event_json, status,
                frame_blob, frame_format, retention_class,
                interaction_request_id, interaction_kind, interaction_status, anchor_seq,
                projection_state,
                runtime_source_seq
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(session_id or ""),
                str(run_id or ""),
                str(turn_id or ""),
                str(execution_session_id or ""),
                str(runtime_scope_key or ""),
                str(participant_id or ""),
                activity_id,
                str(event_type or ""),
                int(seq),
                float(timestamp),
                payload_json,
                event_json,
                str(status or ""),
                frame_blob,
                str(frame_format or ""),
                str(retention_class or ""),
                interaction_request_id,
                interaction_kind,
                interaction_status,
                int(anchor_seq or 0),
                str(projection_state or "raw"),
                int(runtime_source_seq or 0),
            ),
        )

    def mark_projected_tool_event(self, *, row_id: int, tool_event_id: str) -> None:
        if int(row_id or 0) <= 0 or not str(tool_event_id or "").strip():
            return
        self._conn.execute(
            """
            UPDATE run_events
            SET projected_tool_event_id = ?,
                projection_state = COALESCE(NULLIF(projection_state, ''), 'raw')
            WHERE id = ?
            """,
            (str(tool_event_id or "").strip(), int(row_id)),
        )

    def mark_projected_message(self, *, row_id: int, conversation_message_id: str) -> None:
        if int(row_id or 0) <= 0 or not str(conversation_message_id or "").strip():
            return
        self._conn.execute(
            """
            UPDATE run_events
            SET projected_message_id = ?,
                projection_state = 'projected'
            WHERE id = ?
            """,
            (str(conversation_message_id or "").strip(), int(row_id)),
        )

    def mark_activity_id(self, *, row_id: int, activity_id: str) -> None:
        if int(row_id or 0) <= 0 or not str(activity_id or "").strip():
            return
        self._conn.execute(
            """
            UPDATE run_events
            SET activity_id = ?
            WHERE id = ?
            """,
            (str(activity_id or "").strip(), int(row_id)),
        )

    def update_runtime_source_seq(self, *, row_id: int, runtime_source_seq: int) -> None:
        if int(row_id or 0) <= 0:
            return
        self._conn.execute(
            """
            UPDATE run_events
            SET runtime_source_seq = ?
            WHERE id = ?
            """,
            (int(runtime_source_seq or 0), int(row_id)),
        )

    def update_frame_columns(
        self,
        *,
        row_id: int,
        frame_blob: bytes | None,
        frame_format: str,
        retention_class: str = "",
        projected_message_id: str = "",
        projected_tool_event_id: str = "",
        projection_state: str = "",
    ) -> None:
        if int(row_id or 0) <= 0:
            return
        self._conn.execute(
            """
            UPDATE run_events
            SET frame_blob = ?,
                frame_format = ?,
                retention_class = COALESCE(NULLIF(?, ''), retention_class),
                projected_message_id = COALESCE(NULLIF(?, ''), projected_message_id),
                projected_tool_event_id = COALESCE(NULLIF(?, ''), projected_tool_event_id),
                projection_state = COALESCE(NULLIF(?, ''), projection_state)
            WHERE id = ?
            """,
            (
                frame_blob,
                str(frame_format or ""),
                str(retention_class or ""),
                str(projected_message_id or ""),
                str(projected_tool_event_id or ""),
                str(projection_state or ""),
                int(row_id),
            ),
        )

    def rewrite_referenced_frame(
        self,
        *,
        row_id: int,
        payload_json: str,
        event_json: str,
        frame_blob: bytes | None,
        frame_format: str,
        projected_message_id: str = "",
        projected_tool_event_id: str = "",
        runtime_source_seq: int = 0,
    ) -> None:
        if int(row_id or 0) <= 0:
            return
        self._conn.execute(
            """
            UPDATE run_events
            SET payload_json = ?,
                event_json = ?,
                frame_blob = ?,
                frame_format = ?,
                projected_message_id = COALESCE(NULLIF(?, ''), projected_message_id),
                projected_tool_event_id = COALESCE(NULLIF(?, ''), projected_tool_event_id),
                projection_state = 'referenced',
                runtime_source_seq = ?
            WHERE id = ?
            """,
            (
                str(payload_json or ""),
                str(event_json or ""),
                frame_blob,
                str(frame_format or ""),
                str(projected_message_id or ""),
                str(projected_tool_event_id or ""),
                int(runtime_source_seq or 0),
                int(row_id),
            ),
        )

    def rewrite_runtime_frame_row(
        self,
        *,
        row_id: int,
        run_id: str,
        turn_id: str,
        execution_session_id: str,
        runtime_scope_key: str,
        participant_id: str,
        activity_id: str | None,
        seq: int,
        timestamp: float,
        payload_json: str,
        event_json: str,
        status: str,
        frame_blob: bytes | None,
        frame_format: str,
        retention_class: str,
        runtime_source_seq: int = 0,
    ) -> None:
        """Rewrite one existing row during ledger-owned compaction/coalescing.

        This is not a public append path: it preserves the current row id and is
        reserved for maintenance flows that merge multiple runtime fragments
        into a single canonical frame.
        """

        if int(row_id or 0) <= 0:
            return
        self._conn.execute(
            """
            UPDATE run_events
            SET run_id = ?,
                turn_id = ?,
                execution_session_id = ?,
                runtime_scope_key = ?,
                participant_id = ?,
                activity_id = ?,
                seq = ?,
                timestamp = ?,
                payload_json = ?,
                event_json = ?,
                status = ?,
                frame_blob = ?,
                frame_format = ?,
                retention_class = ?,
                projection_state = COALESCE(NULLIF(projection_state, ''), 'raw'),
                runtime_source_seq = ?
            WHERE id = ?
            """,
            (
                str(run_id or ""),
                str(turn_id or ""),
                str(execution_session_id or ""),
                str(runtime_scope_key or ""),
                str(participant_id or ""),
                activity_id,
                int(seq),
                float(timestamp),
                str(payload_json or ""),
                str(event_json or ""),
                str(status or ""),
                frame_blob,
                str(frame_format or ""),
                str(retention_class or ""),
                int(runtime_source_seq or 0),
                int(row_id),
            ),
        )

    def rewrite_compacted_frame_row(
        self,
        *,
        row_id: int,
        seq: int,
        participant_id: str,
        payload_json: str,
        event_json: str,
        frame_blob: bytes | None,
        frame_format: str,
        retention_class: str,
        runtime_source_seq: int = 0,
        run_id: str | None = None,
        turn_id: str | None = None,
        execution_session_id: str | None = None,
        runtime_scope_key: str | None = None,
        activity_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Rewrite a retained row after pruning or stream compaction.

        Optional identity fields are only touched when provided; this keeps
        older compaction callers from accidentally blanking transcript anchors.
        """

        if int(row_id or 0) <= 0:
            return
        assignments = [
            "seq = ?",
            "participant_id = ?",
            "payload_json = ?",
            "event_json = ?",
            "frame_blob = ?",
            "frame_format = ?",
            "retention_class = COALESCE(NULLIF(retention_class, ''), ?)",
            "projection_state = COALESCE(NULLIF(projection_state, ''), 'raw')",
            "runtime_source_seq = ?",
        ]
        params: list[Any] = [
            int(seq),
            str(participant_id or ""),
            str(payload_json or ""),
            str(event_json or ""),
            frame_blob,
            str(frame_format or ""),
            str(retention_class or ""),
            int(runtime_source_seq or 0),
        ]
        if run_id is not None:
            assignments.append("run_id = ?")
            params.append(str(run_id or ""))
        if turn_id is not None:
            assignments.append("turn_id = ?")
            params.append(str(turn_id or ""))
        if execution_session_id is not None:
            assignments.append("execution_session_id = ?")
            params.append(str(execution_session_id or ""))
        if runtime_scope_key is not None:
            assignments.append("runtime_scope_key = ?")
            params.append(str(runtime_scope_key or ""))
        if activity_id is not None:
            assignments.append("activity_id = ?")
            params.append(str(activity_id or ""))
        if timestamp is not None:
            assignments.append("timestamp = ?")
            params.append(float(timestamp or 0))
        params.append(int(row_id))
        self._conn.execute(
            f"""
            UPDATE run_events
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            tuple(params),
        )

    def delete_rows_by_id(self, row_ids: Iterable[int]) -> int:
        ids = [int(row_id) for row_id in row_ids if int(row_id or 0) > 0]
        if not ids:
            return 0
        deleted = 0
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self._conn.execute(
                f"DELETE FROM run_events WHERE id IN ({placeholders})",
                tuple(chunk),
            )
            deleted += int(cursor.rowcount or 0)
        return deleted

    def archive_rows(self, rows: Iterable[sqlite3.Row], *, reason: str) -> int:
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (str(row["session_id"] or ""), str(row["run_id"] or ""))
            grouped.setdefault(key, []).append(row)
        archived_at = time.time()
        archived = 0
        for (session_id, run_id), group in grouped.items():
            seqs = [int(row["seq"] or 0) for row in group]
            timestamps = [float(row["timestamp"] or 0) for row in group]
            self._conn.execute(
                """
                INSERT INTO run_event_archives (
                    session_id, run_id, archived_at, first_seq, last_seq,
                    first_timestamp, last_timestamp, event_count, reason,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    archived_at,
                    min(seqs),
                    max(seqs),
                    min(timestamps),
                    max(timestamps),
                    len(group),
                    str(reason or ""),
                    _dumps({"policy": "run_event_retention"}),
                ),
            )
            archived += len(group)
        return archived

    def delete_sessions(self, session_ids: Iterable[str]) -> int:
        ids = [str(session_id or "").strip() for session_id in session_ids if str(session_id or "").strip()]
        if not ids:
            return 0
        deleted = 0
        for start in range(0, len(ids), 250):
            chunk = ids[start:start + 250]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self._conn.execute(
                f"""
                DELETE FROM run_events
                WHERE session_id IN ({placeholders})
                   OR execution_session_id IN ({placeholders})
                """,
                tuple(chunk + chunk),
            )
            deleted += int(cursor.rowcount or 0)
        return deleted

    def list_runtime_rows(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int = 0,
        active_only: bool = False,
        active_statuses: Iterable[str] = (),
        runtime_scope_key: str = "",
        run_id: str = "",
        activity_id: str = "",
        event_types: Iterable[str] = (),
        exclude_event_types: Iterable[str] = (),
        exclude_event_type_prefixes: Iterable[str] = (),
        include_internal: bool = False,
        limit: int = 2000,
    ) -> list[Any]:
        """Return physical ``run_events`` rows for canonical replay.

        This keeps the query contract in the ledger while callers finish their
        own row decoding or projection work.
        """

        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 5000))
        clauses = ["session_id = ?"]
        params: list[Any] = [stable_sid]
        normalized_after_seq = int(after_seq or 0)
        normalized_before_seq = int(before_seq or 0)
        if normalized_after_seq > 0:
            clauses.append("seq > ?")
            params.append(normalized_after_seq)
        if normalized_before_seq > 0:
            clauses.append("seq < ?")
            params.append(normalized_before_seq)
        scope = str(runtime_scope_key or "").strip()
        if scope:
            clauses.append("COALESCE(runtime_scope_key, session_id) = ?")
            params.append(scope)
        normalized_run_id = str(run_id or "").strip()
        if normalized_run_id:
            clauses.append("run_id = ?")
            params.append(normalized_run_id)
        normalized_activity_id = str(activity_id or "").strip()
        if normalized_activity_id:
            clauses.append("activity_id = ?")
            params.append(normalized_activity_id)
        included_types = _normalized_text_values(event_types)
        if included_types:
            clauses.append(
                f"event_type IN ({','.join('?' for _ in included_types)})"
            )
            params.extend(included_types)
        excluded_types = _normalized_text_values(exclude_event_types)
        if excluded_types:
            clauses.append(
                f"event_type NOT IN ({','.join('?' for _ in excluded_types)})"
            )
            params.extend(excluded_types)
        for prefix in _normalized_text_values(exclude_event_type_prefixes):
            clauses.append("event_type NOT LIKE ?")
            params.append(f"{prefix}%")
        if active_only:
            statuses = [str(status or "").strip() for status in active_statuses if str(status or "").strip()]
            if not statuses:
                return []
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(
                "run_id IN ("
                "SELECT run_id FROM runs WHERE session_id = ? "
                f"AND status IN ({placeholders})"
                ")"
            )
            params.append(stable_sid)
            params.extend(statuses)
        if not include_internal:
            clauses.append("event_type NOT LIKE '_internal.%'")
        params.append(bounded_limit)
        reverse_page = normalized_before_seq > 0 and normalized_after_seq <= 0
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM run_events
            WHERE {' AND '.join(clauses)}
            ORDER BY seq {'DESC' if reverse_page else 'ASC'}
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        ordered = list(rows)
        if reverse_page:
            ordered.reverse()
        return ordered

    def list_run_rows(
        self,
        run_ids: Iterable[str],
        *,
        limit_per_run: int = 2000,
        include_internal: bool = False,
    ) -> list[Any]:
        """Return canonical rows for multiple runs, bounded per run."""
        normalized_run_ids = list(
            dict.fromkeys(
                str(run_id or "").strip()
                for run_id in run_ids
                if str(run_id or "").strip()
            )
        )
        if not normalized_run_ids:
            return []
        bounded_limit = max(1, min(int(limit_per_run or 2000), 5000))
        rows: list[Any] = []
        for start in range(0, len(normalized_run_ids), 250):
            chunk = normalized_run_ids[start:start + 250]
            placeholders = ",".join("?" for _ in chunk)
            internal_clause = "" if include_internal else "AND event_type NOT LIKE '_internal.%'"
            rows.extend(
                self._conn.execute(
                    f"""
                    WITH ranked AS (
                        SELECT *,
                               ROW_NUMBER() OVER (
                                   PARTITION BY run_id
                                   ORDER BY seq ASC, id ASC
                               ) AS run_row_number
                          FROM run_events
                         WHERE run_id IN ({placeholders})
                           {internal_clause}
                    )
                    SELECT *
                      FROM ranked
                     WHERE run_row_number <= ?
                     ORDER BY run_id ASC, seq ASC, id ASC
                    """,
                    (*chunk, bounded_limit),
                ).fetchall()
            )
        rows.sort(
            key=lambda row: (
                str(row["run_id"] or ""),
                int(row["seq"] or 0),
                int(row["id"] or 0),
            )
        )
        return rows

    def latest_run_row(
        self,
        run_id: str,
        *,
        include_internal: bool = True,
    ) -> Any | None:
        stable_run_id = str(run_id or "").strip()
        if not stable_run_id:
            return None
        internal_clause = "" if include_internal else "AND event_type NOT LIKE '_internal.%'"
        return self._conn.execute(
            f"""
            SELECT *
              FROM run_events
             WHERE run_id = ?
               {internal_clause}
             ORDER BY seq DESC, id DESC
             LIMIT 1
            """,
            (stable_run_id,),
        ).fetchone()

    def list_activity_rows(
        self,
        activity_id: str,
        *,
        after_seq: int = 0,
        include_internal: bool = False,
        limit: int = 2000,
    ) -> list[Any]:
        normalized_activity_id = str(activity_id or "").strip()
        if not normalized_activity_id:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 5000))
        clauses = ["activity_id = ?", "seq > ?"]
        params: list[Any] = [normalized_activity_id, int(after_seq or 0)]
        if not include_internal:
            clauses.append("event_type NOT LIKE '_internal.%'")
        params.append(bounded_limit)
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM run_events
            WHERE {' AND '.join(clauses)}
            ORDER BY seq ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return list(rows)

    def list_mission_activity_rows(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        include_internal: bool = False,
        limit: int = 2000,
        reverse: bool = False,
    ) -> list[Any]:
        stable_mission = str(mission_id or "").strip()
        if not stable_mission:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 5000))
        clauses = [
            "(activity_id = ? OR activity_id LIKE ?)",
            "seq > ?",
        ]
        params: list[Any] = [
            f"mission:{stable_mission}",
            f"act-node:{stable_mission}:%",
            int(after_seq or 0),
        ]
        if not include_internal:
            clauses.append("event_type NOT LIKE '_internal.%'")
        params.append(bounded_limit)
        order_direction = "DESC" if reverse else "ASC"
        outer_direction = "ASC"
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM (
                SELECT *
                FROM run_events
                WHERE {' AND '.join(clauses)}
                ORDER BY seq {order_direction}, id {order_direction}
                LIMIT ?
            )
            ORDER BY seq {outer_direction}, id {outer_direction}
            """,
            tuple(params),
        ).fetchall()
        return list(rows)

    def list_tool_event_projection_rows(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        run_id: str = "",
        direction: str = "after",
        limit: int = 2000,
    ) -> list[Any]:
        """Read legacy ``tool_events`` projection rows.

        ``tool_events`` is a read model derived from ``run_events``. Keeping
        this accessor on the ledger prevents callers from treating
        the projection as an independent storage owner.
        """

        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 5000))
        normalized_direction = str(direction or "after").strip().lower()
        clauses = ["session_id = ?", "COALESCE(seq_last, seq_start, 0) > ?"]
        params: list[Any] = [stable_sid, int(after_seq or 0)]
        normalized_run_id = str(run_id or "").strip()
        if normalized_run_id:
            clauses.append("COALESCE(run_id, '') = ?")
            params.append(normalized_run_id)
        params.append(bounded_limit)
        order_expr = "COALESCE(seq_start, seq_last, id)"
        if normalized_direction == "tail":
            rows = self._conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT *
                    FROM tool_events
                    WHERE {' AND '.join(clauses)}
                    ORDER BY {order_expr} DESC, id DESC
                    LIMIT ?
                )
                ORDER BY {order_expr} ASC, id ASC
                """,
                tuple(params),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"""
                SELECT *
                FROM tool_events
                WHERE {' AND '.join(clauses)}
                ORDER BY {order_expr} ASC, id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return list(rows)

    def list_frame_backfill_rows(
        self,
        *,
        session_id: str = "",
        limit: int = 5000,
    ) -> list[Any]:
        stable = str(session_id or "").strip()
        session_clause = "AND session_id = ?" if stable else ""
        params: list[Any] = [stable] if stable else []
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM run_events
            WHERE (
                frame_blob IS NULL
                OR COALESCE(frame_format, '') = ''
                OR COALESCE(retention_class, '') = ''
                OR NOT EXISTS (
                    SELECT 1
                    FROM run_event_search_index idx
                    WHERE idx.run_event_id = run_events.id
                )
            )
              {session_clause}
            ORDER BY session_id ASC, seq ASC, id ASC
            LIMIT ?
            """,
            (*params, max(1, min(int(limit or 5000), 20000))),
        ).fetchall()
        return list(rows)

    def list_activity_id_backfill_rows(
        self,
        *,
        after_id: int = 0,
        limit: int = 5000,
    ) -> list[Any]:
        return list(
            self._conn.execute(
                """
                SELECT id, session_id, event_json
                  FROM run_events
                 WHERE activity_id IS NULL
                   AND id > ?
                 ORDER BY id ASC
                 LIMIT ?
                """,
                (max(0, int(after_id or 0)), max(1, int(limit or 5000))),
            ).fetchall()
        )

    def has_activity_id_backfill_rows(self, *, after_id: int = 0) -> bool:
        row = self._conn.execute(
            """
            SELECT 1
              FROM run_events
             WHERE activity_id IS NULL
               AND id > ?
             LIMIT 1
            """,
            (max(0, int(after_id or 0)),),
        ).fetchone()
        return row is not None

    def count_frame_backfill_rows(self, *, session_id: str = "") -> int:
        stable = str(session_id or "").strip()
        session_clause = "AND session_id = ?" if stable else ""
        row = self._conn.execute(
            f"""
            SELECT COUNT(1) AS count
            FROM run_events
            WHERE (
                frame_blob IS NULL
                OR COALESCE(frame_format, '') = ''
                OR COALESCE(retention_class, '') = ''
                OR NOT EXISTS (
                    SELECT 1
                    FROM run_event_search_index idx
                    WHERE idx.run_event_id = run_events.id
                )
            )
              {session_clause}
            """,
            (stable,) if stable else (),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_filtered_rows(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        runtime_scope_key: str = "",
        event_types: Iterable[str] = (),
        event_type_prefix: str = "",
        payload_contains: str = "",
        limit: int = 2000,
    ) -> list[Any]:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 20000))
        clauses = ["e.session_id = ?", "e.seq > ?"]
        params: list[Any] = [stable_sid, int(after_seq or 0)]
        scope = str(runtime_scope_key or "").strip()
        if scope:
            clauses.append("COALESCE(e.runtime_scope_key, e.session_id) = ?")
            params.append(scope)
        normalized_types = [
            str(item or "").strip()
            for item in event_types
            if str(item or "").strip()
        ]
        if normalized_types:
            placeholders = ", ".join("?" for _ in normalized_types)
            clauses.append(f"e.event_type IN ({placeholders})")
            params.extend(normalized_types)
        else:
            prefix = str(event_type_prefix or "").strip()
            if prefix:
                clauses.append("e.event_type LIKE ?")
                params.append(f"{prefix}%")
        contains = str(payload_contains or "").strip()
        from_sql = "run_events e"
        if contains:
            from_sql = (
                "run_events e "
                "JOIN run_event_search_index idx ON idx.run_event_id = e.id"
            )
            clauses.append("instr(idx.search_text, ?) > 0")
            params.append(contains)
        params.append(bounded_limit)
        rows = self._conn.execute(
            f"""
            SELECT e.*
            FROM {from_sql}
            WHERE {' AND '.join(clauses)}
            ORDER BY e.seq ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return list(rows)

    def interaction_anchor_seq(
        self,
        session_id: str,
        request_id: str,
    ) -> int:
        stable_sid = str(session_id or "").strip()
        stable_request_id = str(request_id or "").strip()
        if not stable_sid or not stable_request_id:
            return 0
        row = self._conn.execute(
            """
            SELECT anchor_seq
              FROM run_events
             WHERE session_id = ?
               AND interaction_request_id = ?
               AND event_type = ?
             ORDER BY seq ASC
             LIMIT 1
            """,
            (
                stable_sid,
                stable_request_id,
                InternalRunEventType.INTERACTION_REQUESTED.value,
            ),
        ).fetchone()
        return int(row["anchor_seq"] or 0) if row is not None else 0

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
            return allocate_only(self._conn, session_id=session_id, updated_at=now)
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
        activity_id: str = "",
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO run_events (
                session_id, run_id, seq, event_type, turn_id, timestamp,
                payload_json, event_json, activity_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                seq,
                event_type,
                turn_id,
                ts,
                payload_json,
                event_json,
                str(activity_id or "") or None,
            ),
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
