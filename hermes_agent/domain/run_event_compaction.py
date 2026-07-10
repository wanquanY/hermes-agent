"""Transactional compaction for the canonical run-event ledger."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_event_codec import decode_run_event_row, encode_run_event_frame
from hermes_agent.domain.run_event_index import (
    project_run_event_search_index,
    runtime_source_seq_from_event,
)
from hermes_agent.domain.run_state_machine import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES
from hermes_agent.repositories.run_repo import RunRepoImpl
from hermes_team_mission.runtime.run_event_retention import (
    COALESCIBLE_STREAM_EVENT_TYPES,
    RunEventRetentionPolicy,
)


_TERMINAL_EVENT_TYPES = ("message.complete", "error", "session.interrupted")
_STREAM_BOUNDARY_EVENT_TYPES = frozenset(_TERMINAL_EVENT_TYPES) | {"message.start"}
_SUBAGENT_STREAM_TYPES = frozenset(
    {"subagent.output_delta", "subagent.reasoning_delta", "subagent.thinking"}
)
_SUBAGENT_BOUNDARY_EVENT_TYPES = frozenset(
    {"subagent.start", "subagent.tool", "subagent.complete", "subagent.error"}
)
_STREAM_IDENTITY_PAYLOAD_KEYS = (
    "subagent_id",
    "subagentId",
    "source",
    "role",
    "delegate_call_id",
    "delegateCallId",
    "tool_call_id",
    "toolCallId",
)


@dataclass
class _CompactionStats:
    compacted_segments: int = 0
    pruned_terminal_stream_events: int = 0
    deduplicated_terminal_groups: int = 0
    deleted_events: int = 0
    updated_events: int = 0
    affected_run_ids: set[str] = field(default_factory=set)

    def to_result(self) -> dict[str, int | bool]:
        return {
            "compacted_segments": self.compacted_segments,
            "pruned_terminal_stream_events": self.pruned_terminal_stream_events,
            "deduplicated_terminal_groups": self.deduplicated_terminal_groups,
            "deleted_events": self.deleted_events,
            "updated_events": self.updated_events,
            "vacuumed": False,
        }


class RunEventCompactor:
    """Coalesce replay-only stream rows while preserving durable facts."""

    def __init__(self, retention: RunEventRetentionPolicy | None = None) -> None:
        self._retention = retention or RunEventRetentionPolicy()

    def compact(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str = "",
    ) -> dict[str, int | bool]:
        stable_filter = str(session_id or "").strip()
        ledger = EventLedger(conn)
        stats = _CompactionStats()

        self._prune_terminal_streams(conn, ledger, stable_filter, stats)
        self._deduplicate_terminal_groups(conn, ledger, stable_filter, stats)
        self._coalesce_stream_segments(conn, ledger, stable_filter, stats)
        run_repo = RunRepoImpl(conn)
        for run_id in stats.affected_run_ids:
            run_repo.reset_last_seq_from_events(run_id)
        return stats.to_result()

    def _prune_terminal_streams(
        self,
        conn: sqlite3.Connection,
        ledger: EventLedger,
        session_id: str,
        stats: _CompactionStats,
    ) -> None:
        event_types = self._retention.terminal_prunable_event_types()
        if not event_types:
            return
        type_placeholders = ",".join("?" for _ in event_types)
        params: list[Any] = [*event_types]
        session_clause = ""
        if session_id:
            session_clause = "AND e.session_id = ?"
            params.append(session_id)
        rows = conn.execute(
            f"""
            SELECT e.*
              FROM run_events e
              LEFT JOIN runs r ON r.run_id = e.run_id
             WHERE e.event_type IN ({type_placeholders})
               AND {_terminal_run_predicate()}
               {session_clause}
             ORDER BY e.session_id ASC, e.seq ASC, e.id ASC
            """,
            tuple(params),
        ).fetchall()
        deletable = [
            row for row in rows if self._retention.can_delete_terminal_stream_row(conn, row)
        ]
        if not deletable:
            return
        ledger.archive_rows(deletable, reason="terminal_run_stream_events")
        ledger.delete_rows_by_id(int(row["id"]) for row in deletable)
        for row in deletable:
            run_id = str(row["run_id"] or "").strip()
            if run_id:
                stats.affected_run_ids.add(run_id)
        stats.pruned_terminal_stream_events += len(deletable)
        stats.deleted_events += len(deletable)

    def _deduplicate_terminal_groups(
        self,
        conn: sqlite3.Connection,
        ledger: EventLedger,
        session_id: str,
        stats: _CompactionStats,
    ) -> None:
        event_placeholders = ",".join("?" for _ in _TERMINAL_EVENT_TYPES)
        terminal_statuses = tuple(sorted(TERMINAL_RUN_STATUSES))
        status_placeholders = ",".join("?" for _ in terminal_statuses)
        active_statuses = tuple(sorted(ACTIVE_RUN_STATUSES))
        active_placeholders = ",".join("?" for _ in active_statuses)
        params: list[Any] = [
            *_TERMINAL_EVENT_TYPES,
            *terminal_statuses,
            *active_statuses,
        ]
        session_clause = ""
        if session_id:
            session_clause = "AND e.session_id = ?"
            params.append(session_id)
        groups = conn.execute(
            f"""
            SELECT e.session_id,
                   COALESCE(e.run_id, '') AS run_id,
                   COALESCE(e.turn_id, '') AS turn_id,
                   e.event_type,
                   COALESCE(e.status, '') AS status,
                   COUNT(*) AS event_count
              FROM run_events e
              LEFT JOIN runs r ON r.run_id = e.run_id
             WHERE e.event_type IN ({event_placeholders})
               AND COALESCE(e.status, '') IN ({status_placeholders})
               AND COALESCE(r.status, '') NOT IN ({active_placeholders})
               {session_clause}
             GROUP BY e.session_id, COALESCE(e.run_id, ''),
                      COALESCE(e.turn_id, ''), e.event_type,
                      COALESCE(e.status, '')
            HAVING COUNT(*) > 1
            """,
            tuple(params),
        ).fetchall()
        for group in groups:
            rows = conn.execute(
                """
                SELECT *
                  FROM run_events
                 WHERE session_id = ?
                   AND COALESCE(run_id, '') = ?
                   AND COALESCE(turn_id, '') = ?
                   AND event_type = ?
                   AND COALESCE(status, '') = ?
                 ORDER BY seq ASC, id ASC
                """,
                (
                    group["session_id"],
                    group["run_id"],
                    group["turn_id"],
                    group["event_type"],
                    group["status"],
                ),
            ).fetchall()
            if len(rows) <= 1:
                continue
            keep_row = rows[-1]
            event = decode_run_event_row(keep_row)
            canonical_seq = int(rows[0]["seq"] or keep_row["seq"] or 0)
            event = {**event, "seq": canonical_seq}
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            participant_id = _event_participant_id(event)
            frame_blob, frame_format = encode_run_event_frame(event)
            runtime_source_seq = runtime_source_seq_from_event(event)
            delete_ids = [int(row["id"]) for row in rows[:-1]]
            ledger.delete_rows_by_id(delete_ids)
            ledger.rewrite_compacted_frame_row(
                row_id=int(keep_row["id"]),
                seq=canonical_seq,
                participant_id=participant_id,
                payload_json=_dumps(payload),
                event_json=_dumps(event),
                frame_blob=frame_blob,
                frame_format=frame_format,
                retention_class=self._retention.classify_event_type(
                    str(group["event_type"] or event.get("type") or "")
                ),
                runtime_source_seq=runtime_source_seq,
            )
            _project_event_index(
                conn,
                keep_row,
                event,
                seq=canonical_seq,
                runtime_source_seq=runtime_source_seq,
            )
            run_id = str(group["run_id"] or "").strip()
            if run_id:
                stats.affected_run_ids.add(run_id)
            stats.deduplicated_terminal_groups += 1
            stats.deleted_events += len(delete_ids)
            stats.updated_events += 1

    def _coalesce_stream_segments(
        self,
        conn: sqlite3.Connection,
        ledger: EventLedger,
        session_id: str,
        stats: _CompactionStats,
    ) -> None:
        active_statuses = tuple(sorted(ACTIVE_RUN_STATUSES))
        placeholders = ",".join("?" for _ in active_statuses)
        params: list[Any] = [*active_statuses]
        session_clause = ""
        if session_id:
            session_clause = "AND e.session_id = ?"
            params.append(session_id)
        rows = conn.execute(
            f"""
            SELECT e.*
              FROM run_events e
              LEFT JOIN runs r ON r.run_id = e.run_id
             WHERE COALESCE(r.status, '') NOT IN ({placeholders})
               {session_clause}
             ORDER BY e.session_id ASC, e.seq ASC, e.id ASC
            """,
            tuple(params),
        ).fetchall()
        pending_by_key: dict[
            tuple[Any, ...], list[tuple[sqlite3.Row, dict[str, Any]]]
        ] = {}

        def flush(key: tuple[Any, ...]) -> None:
            pending = pending_by_key.pop(key, [])
            if len(pending) <= 1:
                return
            merged = pending[0][1]
            for _, event in pending[1:]:
                participant_id = _event_participant_id(
                    event, _event_participant_id(merged)
                )
                merged = {
                    **merged,
                    "session_id": event.get("session_id") or merged.get("session_id") or "",
                    "conversation_session_id": event.get("conversation_session_id")
                    or merged.get("conversation_session_id")
                    or "",
                    "run_id": _event_run_id(event) or _event_run_id(merged),
                    "turn_id": _event_turn_id(event) or _event_turn_id(merged),
                    "execution_session_id": event.get("execution_session_id")
                    or merged.get("execution_session_id")
                    or "",
                    "runtime_scope_key": _event_runtime_scope_key(
                        event, _event_runtime_scope_key(merged)
                    ),
                    "participant_id": participant_id,
                    "participantId": participant_id,
                    "seq": int(event.get("seq") or merged.get("seq") or 0),
                    "timestamp": float(
                        event.get("timestamp") or merged.get("timestamp") or 0
                    ),
                    "payload": _merge_stream_payload(merged, event),
                }
            keep_row = pending[-1][0]
            delete_ids = [int(row["id"]) for row, _ in pending[:-1]]
            ledger.delete_rows_by_id(delete_ids)
            payload = merged.get("payload") if isinstance(merged.get("payload"), dict) else {}
            participant_id = _event_participant_id(merged)
            if participant_id:
                payload = dict(payload)
                payload.setdefault("participant_id", participant_id)
                merged["payload"] = payload
            frame_blob, frame_format = encode_run_event_frame(merged)
            runtime_source_seq = runtime_source_seq_from_event(merged)
            ledger.rewrite_compacted_frame_row(
                row_id=int(keep_row["id"]),
                run_id=_event_run_id(merged),
                turn_id=_event_turn_id(merged),
                execution_session_id=str(merged.get("execution_session_id") or ""),
                runtime_scope_key=_event_runtime_scope_key(merged),
                participant_id=participant_id,
                seq=int(merged.get("seq") or 0),
                timestamp=float(merged.get("timestamp") or 0),
                payload_json=_dumps(payload),
                event_json=_dumps(merged),
                frame_blob=frame_blob,
                frame_format=frame_format,
                retention_class=self._retention.classify_event_type(
                    str(merged.get("type") or "")
                ),
                runtime_source_seq=runtime_source_seq,
            )
            _project_event_index(
                conn,
                keep_row,
                merged,
                seq=int(merged.get("seq") or 0),
                runtime_source_seq=runtime_source_seq,
            )
            run_id = _event_run_id(merged)
            if run_id:
                stats.affected_run_ids.add(run_id)
            stats.compacted_segments += 1
            stats.deleted_events += len(delete_ids)
            stats.updated_events += 1

        def flush_all() -> None:
            for key in list(pending_by_key):
                flush(key)

        for row in rows:
            event = decode_run_event_row(row)
            if not _is_coalescible_stream(event):
                boundary_types = _boundary_stream_types(
                    str(row["event_type"] or event.get("type") or "")
                )
                if boundary_types is None:
                    flush_all()
                elif boundary_types:
                    for key in list(pending_by_key):
                        if len(key) > 1 and key[1] in boundary_types:
                            flush(key)
                continue
            key = (str(row["session_id"] or ""), *_stream_identity(event))
            pending = pending_by_key.get(key, [])
            if pending and _events_can_coalesce(pending[-1][1], event):
                pending.append((row, event))
                continue
            flush(key)
            pending_by_key[key] = [(row, event)]
        flush_all()


def _terminal_run_predicate() -> str:
    terminal_statuses = _sql_literals(TERMINAL_RUN_STATUSES)
    terminal_event_types = _sql_literals(set(_TERMINAL_EVENT_TYPES))
    return f"""
    (
        COALESCE(r.status, '') IN ({terminal_statuses})
        OR (
            COALESCE(e.run_id, '') != ''
            AND EXISTS (
                SELECT 1
                  FROM run_events terminal_events
                 WHERE terminal_events.session_id = e.session_id
                   AND COALESCE(terminal_events.run_id, '') = COALESCE(e.run_id, '')
                   AND terminal_events.event_type IN ({terminal_event_types})
                   AND COALESCE(terminal_events.status, '') IN ({terminal_statuses})
            )
        )
    )
    """


def _sql_literals(values: set[str]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def _event_run_id(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(event.get("run_id") or payload.get("run_id") or "").strip()


def _event_turn_id(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(event.get("turn_id") or payload.get("turn_id") or "").strip()


def _event_runtime_scope_key(event: dict[str, Any], fallback: str = "") -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        event.get("runtime_scope_key")
        or payload.get("runtime_scope_key")
        or fallback
        or ""
    ).strip()


def _event_participant_id(event: dict[str, Any], fallback: str = "") -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        event.get("participant_id")
        or event.get("participantId")
        or payload.get("participant_id")
        or payload.get("participantId")
        or fallback
        or ""
    ).strip()


def _stream_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(payload.get("text") or payload.get("delta") or payload.get("output") or "")


def _is_coalescible_stream(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") in COALESCIBLE_STREAM_EVENT_TYPES and bool(
        _stream_text(event)
    )


def _stream_identity(event: dict[str, Any]) -> tuple[Any, ...]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    identity: list[Any] = [
        str(event.get("type") or "").strip(),
        _event_run_id(event),
        _event_turn_id(event),
        _event_runtime_scope_key(event),
        str(payload.get("mode") or "").strip().lower(),
    ]
    identity.extend(str(payload.get(key) or "").strip() for key in _STREAM_IDENTITY_PAYLOAD_KEYS)
    return tuple(identity)


def _events_can_coalesce(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    return (
        _is_coalescible_stream(previous)
        and _is_coalescible_stream(current)
        and _stream_identity(previous) == _stream_identity(current)
    )


def _boundary_stream_types(event_type: str) -> set[str] | None:
    normalized = str(event_type or "").strip()
    if normalized in _STREAM_BOUNDARY_EVENT_TYPES:
        return None
    if normalized in _SUBAGENT_BOUNDARY_EVENT_TYPES:
        return set(_SUBAGENT_STREAM_TYPES)
    return set()


def _merge_stream_payload(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    previous_payload = (
        previous.get("payload") if isinstance(previous.get("payload"), dict) else {}
    )
    current_payload = (
        current.get("payload") if isinstance(current.get("payload"), dict) else {}
    )
    merged = dict(previous_payload)
    text = _merge_stream_text(_stream_text(previous), _stream_text(current))
    for key in ("text", "delta", "output"):
        if key in previous_payload or key in current_payload:
            merged[key] = text
    merged.pop("rendered", None)
    return merged


def _merge_stream_text(previous: str, incoming: str) -> str:
    if not incoming or incoming == previous or incoming in previous:
        return previous
    if not previous or incoming.startswith(previous):
        return incoming
    max_overlap = min(len(previous), len(incoming))
    for size in range(max_overlap, 0, -1):
        if previous.endswith(incoming[:size]):
            return previous + incoming[size:]
    return previous + incoming


def _project_event_index(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    event: dict[str, Any],
    *,
    seq: int,
    runtime_source_seq: int,
) -> None:
    project_run_event_search_index(
        conn,
        row_id=int(row["id"]),
        session_id=str(row["session_id"] or event.get("conversation_session_id") or ""),
        seq=int(seq),
        event_type=str(row["event_type"] or event.get("type") or ""),
        runtime_scope_key=str(
            row["runtime_scope_key"] or event.get("runtime_scope_key") or ""
        ),
        runtime_source_seq=int(runtime_source_seq),
        event=event,
        updated_at=float(row["timestamp"] or event.get("timestamp") or 0),
    )


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = ["RunEventCompactor"]
