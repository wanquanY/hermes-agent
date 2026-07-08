from __future__ import annotations

import json
import sqlite3
import zlib
from typing import Any

from hermes_agent.domain.event_ledger import EventLedger


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
    transcript_session_id = _text(_row_value(row, "session_id", ""))
    if transcript_session_id:
        event["session_id"] = transcript_session_id
    else:
        event.setdefault("session_id", "")
    event.setdefault("runtime_scope_key", _row_value(row, "runtime_scope_key", ""))
    event.setdefault("run_id", _row_value(row, "run_id", ""))
    event.setdefault("turn_id", _row_value(row, "turn_id", ""))
    event.setdefault("participant_id", _row_value(row, "participant_id", ""))
    event.setdefault("seq", int(_row_value(row, "seq", 0) or 0))
    event.setdefault("timestamp", float(_row_value(row, "timestamp", 0) or 0))
    interaction_request_id = _text(_row_value(row, "interaction_request_id", ""))
    if interaction_request_id:
        payload.setdefault("interaction_request_id", interaction_request_id)
        payload.setdefault("request_id", interaction_request_id)
        interaction_kind = _text(_row_value(row, "interaction_kind", ""))
        interaction_status = _text(_row_value(row, "interaction_status", ""))
        anchor_seq = int(_row_value(row, "anchor_seq", 0) or 0)
        if interaction_kind:
            payload.setdefault("interaction_kind", interaction_kind)
            payload.setdefault("kind", interaction_kind)
        if interaction_status:
            payload.setdefault("interaction_status", interaction_status)
            payload.setdefault("status", interaction_status)
            payload.setdefault("state", interaction_status)
        if anchor_seq > 0:
            payload["anchor_seq"] = anchor_seq
            event.setdefault("anchor_seq", anchor_seq)
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
    EventLedger(conn).update_frame_columns(
        row_id=int(row_id),
        frame_blob=frame_blob,
        frame_format=frame_format,
        retention_class=retention_class,
        projected_message_id=projected_message_id,
        projected_tool_event_id=projected_tool_event_id,
        projection_state=projection_state,
    )
