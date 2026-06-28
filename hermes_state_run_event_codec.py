from __future__ import annotations

import json
import sqlite3
import zlib
from typing import Any


RUN_EVENT_FRAME_FORMAT = "zlib+json:v1"


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return fallback
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def encode_run_event_frame(event: dict[str, Any]) -> tuple[bytes, str]:
    payload = json_dumps_compact(event).encode("utf-8")
    return zlib.compress(payload, level=6), RUN_EVENT_FRAME_FORMAT


def decode_run_event_frame_blob(blob: Any, frame_format: Any) -> dict[str, Any]:
    if not blob or _text(frame_format) != RUN_EVENT_FRAME_FORMAT:
        return {}
    try:
        raw = bytes(blob)
        decoded = zlib.decompress(raw).decode("utf-8")
    except Exception:
        return {}
    event = json_loads(decoded, {})
    return event if isinstance(event, dict) else {}


def decode_run_event_row(row: Any) -> dict[str, Any]:
    event = decode_run_event_frame_blob(
        _row_value(row, "frame_blob"),
        _row_value(row, "frame_format"),
    )
    if not event:
        event = json_loads(_row_value(row, "event_json"), {})
    event = event if isinstance(event, dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else None
    if payload is None:
        payload = json_loads(_row_value(row, "payload_json"), {})
    payload = payload if isinstance(payload, dict) else {}
    event.setdefault("type", _row_value(row, "event_type", ""))
    event.setdefault("stored_session_id", _row_value(row, "session_id", ""))
    event.setdefault("session_id", _row_value(row, "runtime_session_id", ""))
    event.setdefault("runtime_session_id", _row_value(row, "runtime_session_id", ""))
    event.setdefault("runtime_scope_key", _row_value(row, "runtime_scope_key", ""))
    event.setdefault("run_id", _row_value(row, "run_id", ""))
    event.setdefault("turn_id", _row_value(row, "turn_id", ""))
    event.setdefault("participant_id", _row_value(row, "participant_id", ""))
    event.setdefault("seq", int(_row_value(row, "seq", 0) or 0))
    event.setdefault("timestamp", float(_row_value(row, "timestamp", 0) or 0))
    event["payload"] = payload
    return event


def payload_from_run_event_row(row: Any) -> dict[str, Any]:
    payload = decode_run_event_row(row).get("payload")
    return payload if isinstance(payload, dict) else {}


def update_run_event_frame_columns(
    conn: sqlite3.Connection,
    *,
    row_id: int,
    event: dict[str, Any],
    retention_class: str = "",
    projected_message_id: str = "",
    projected_tool_event_id: str = "",
    projection_state: str = "",
) -> None:
    frame_blob, frame_format = encode_run_event_frame(event)
    conn.execute(
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
            frame_format,
            retention_class,
            projected_message_id,
            projected_tool_event_id,
            projection_state,
            int(row_id),
        ),
    )
