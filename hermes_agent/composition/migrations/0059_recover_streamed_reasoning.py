"""Recover durable assistant reasoning from completed runtime facts."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

version = 59
description = "recover streamed reasoning omitted from assistant messages"

_SEGMENT_SUFFIX = re.compile(r"assistant-segment:(\d+)$")


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return (
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _segment_index(record: dict[str, Any]) -> int | None:
    explicit = _integer(
        record.get("assistant_segment_index")
        if record.get("assistant_segment_index") is not None
        else record.get("assistantSegmentIndex")
    )
    if explicit is not None and explicit >= 0:
        return explicit
    client_message_id = _text(
        record.get("client_message_id") or record.get("clientMessageId")
    )
    match = _SEGMENT_SUFFIX.search(client_message_id)
    if match:
        return int(match.group(1))
    message_seq = _integer(
        record.get("message_seq_in_run")
        if record.get("message_seq_in_run") is not None
        else record.get("messageSeqInRun")
    )
    return message_seq - 1 if message_seq is not None and message_seq > 0 else None


def _reasoning_text(payload: dict[str, Any]) -> str:
    for key in ("text", "snapshot", "delta", "reasoning"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def apply(cursor: sqlite3.Cursor) -> None:
    if not all(_table_exists(cursor, table) for table in ("messages", "run_events")):
        return

    cursor.row_factory = sqlite3.Row
    reasoning_by_segment: dict[tuple[str, str, str, int], tuple[int, str]] = {}
    reasoning_by_run_segment: dict[tuple[str, str, int], tuple[int, str]] = {}
    event_rows = cursor.execute(
        "SELECT session_id, run_id, turn_id, seq, payload_json "
        "FROM run_events WHERE event_type = 'reasoning.available' "
        "ORDER BY session_id, seq"
    ).fetchall()
    for row in event_rows:
        payload = _record(row["payload_json"])
        reasoning = _reasoning_text(payload)
        run_id = _text(row["run_id"] or payload.get("run_id") or payload.get("runId"))
        turn_id = _text(
            row["turn_id"] or payload.get("turn_id") or payload.get("turnId")
        )
        segment_index = _segment_index(payload)
        if not reasoning or not run_id or segment_index is None:
            continue
        session_id = _text(row["session_id"])
        candidate = (int(row["seq"]), reasoning)
        reasoning_by_segment[(session_id, run_id, turn_id, segment_index)] = candidate
        reasoning_by_run_segment[(session_id, run_id, segment_index)] = candidate

    if not reasoning_by_run_segment:
        return

    message_rows = cursor.execute(
        "SELECT id, session_id, reasoning, reasoning_content, metadata_json "
        "FROM messages "
        "WHERE role = 'assistant' "
        "ORDER BY session_id, id"
    ).fetchall()
    ordinal_by_turn: dict[tuple[str, str, str], int] = {}
    for row in message_rows:
        metadata = _record(row["metadata_json"])
        run_id = _text(metadata.get("run_id") or metadata.get("runId"))
        turn_id = _text(metadata.get("turn_id") or metadata.get("turnId"))
        if not run_id:
            continue
        session_id = _text(row["session_id"])
        ordinal_key = (session_id, run_id, turn_id)
        segment_index = _segment_index(metadata)
        if segment_index is None:
            segment_index = ordinal_by_turn.get(ordinal_key, 0)
        ordinal_by_turn[ordinal_key] = max(
            ordinal_by_turn.get(ordinal_key, 0),
            segment_index + 1,
        )
        if _text(row["reasoning"]) or _text(row["reasoning_content"]):
            continue
        recovered = reasoning_by_segment.get(
            (session_id, run_id, turn_id, segment_index)
        ) or reasoning_by_run_segment.get((session_id, run_id, segment_index))
        if recovered is None:
            continue
        event_seq, reasoning = recovered
        metadata["reasoning_recovery"] = {
            "migration_version": version,
            "source_event_seq": event_seq,
        }
        cursor.execute(
            "UPDATE messages SET reasoning = ?, metadata_json = ? WHERE id = ?",
            (
                reasoning,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                int(row["id"]),
            ),
        )


__all__ = ["apply", "description", "version"]
