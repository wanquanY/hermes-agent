from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_state_run_event_codec import decode_run_event_row


MAX_SEARCH_VALUE_CHARS = 2048
MAX_SEARCH_TEXT_CHARS = 65536


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _int_value(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def runtime_source_seq_from_event(event: dict[str, Any]) -> int:
    payload = _record(event.get("payload"))
    source_event = _record(
        payload.get("source_event")
        or payload.get("sourceEvent")
        or payload.get("runtime_event")
        or payload.get("runtimeEvent")
    )
    source_payload = _record(source_event.get("payload"))
    for value in (
        event.get("runtime_source_seq"),
        event.get("runtimeSourceSeq"),
        payload.get("runtime_source_seq"),
        payload.get("runtimeSourceSeq"),
        source_event.get("runtime_source_seq"),
        source_event.get("runtimeSourceSeq"),
        source_event.get("seq"),
        source_payload.get("runtime_source_seq"),
        source_payload.get("runtimeSourceSeq"),
    ):
        parsed = _int_value(value)
        if parsed > 0:
            return parsed
    return 0


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return _text(value)


def _append_scalar_tokens(parts: list[str], prefix: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (str, int, float, bool)):
        text = _text(value)
        if not text:
            return
        if len(text) > MAX_SEARCH_VALUE_CHARS:
            text = text[:MAX_SEARCH_VALUE_CHARS]
        if prefix:
            parts.append(f"{prefix}:{text}")
        parts.append(text)
        return
    if isinstance(value, dict):
        for key in sorted(value):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            _append_scalar_tokens(parts, child_prefix, value.get(key))
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value[:128]):
            child_prefix = f"{prefix}[{index}]" if prefix else str(index)
            _append_scalar_tokens(parts, child_prefix, item)


def run_event_search_text(event: dict[str, Any]) -> str:
    payload = _record(event.get("payload"))
    searchable: dict[str, Any] = {
        "type": event.get("type"),
        "run_id": event.get("run_id"),
        "turn_id": event.get("turn_id"),
        "runtime_session_id": event.get("runtime_session_id") or event.get("session_id"),
        "runtime_scope_key": event.get("runtime_scope_key"),
        "participant_id": event.get("participant_id") or event.get("participantId"),
        "payload": payload,
    }
    parts = [_compact_json(searchable)]
    _append_scalar_tokens(parts, "", searchable)
    text = "\n".join(part for part in parts if part)
    return text[:MAX_SEARCH_TEXT_CHARS] if len(text) > MAX_SEARCH_TEXT_CHARS else text


def project_run_event_search_index(
    conn: sqlite3.Connection,
    *,
    row_id: int,
    session_id: str,
    seq: int,
    event_type: str,
    runtime_scope_key: str,
    runtime_source_seq: int,
    event: dict[str, Any],
    updated_at: float,
) -> None:
    if row_id <= 0 or not session_id:
        return
    conn.execute(
        """
        INSERT INTO run_event_search_index (
            run_event_id, session_id, seq, event_type, runtime_scope_key,
            runtime_source_seq, search_text, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_event_id) DO UPDATE SET
            session_id = excluded.session_id,
            seq = excluded.seq,
            event_type = excluded.event_type,
            runtime_scope_key = excluded.runtime_scope_key,
            runtime_source_seq = excluded.runtime_source_seq,
            search_text = excluded.search_text,
            updated_at = excluded.updated_at
        """,
        (
            int(row_id),
            str(session_id or ""),
            int(seq or 0),
            str(event_type or ""),
            str(runtime_scope_key or ""),
            int(runtime_source_seq or 0),
            run_event_search_text(event),
            float(updated_at or 0),
        ),
    )


def project_run_event_search_index_from_row(conn: sqlite3.Connection, row: Any) -> int:
    if row is None:
        return 0
    event = decode_run_event_row(row)
    runtime_source_seq = runtime_source_seq_from_event(event)
    row_id = int(_row_value(row, "id", 0) or 0)
    if runtime_source_seq != int(_row_value(row, "runtime_source_seq", 0) or 0):
        conn.execute(
            "UPDATE run_events SET runtime_source_seq = ? WHERE id = ?",
            (runtime_source_seq, row_id),
        )
    project_run_event_search_index(
        conn,
        row_id=row_id,
        session_id=str(_row_value(row, "session_id", "") or event.get("stored_session_id") or ""),
        seq=int(_row_value(row, "seq", event.get("seq") or 0) or 0),
        event_type=str(_row_value(row, "event_type", "") or event.get("type") or ""),
        runtime_scope_key=str(
            _row_value(row, "runtime_scope_key", "") or event.get("runtime_scope_key") or ""
        ),
        runtime_source_seq=runtime_source_seq,
        event=event,
        updated_at=float(_row_value(row, "timestamp", event.get("timestamp") or 0) or 0),
    )
    return runtime_source_seq
