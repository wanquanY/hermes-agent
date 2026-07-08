"""Payload-only run event decoding."""

from __future__ import annotations

import json
import zlib
from typing import Any

RUN_EVENT_FRAME_FORMAT = "zlib+json:v1"


def _json_loads(value: Any, fallback: Any = None) -> Any:
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


def payload_from_run_event_row(row: Any) -> dict[str, Any]:
    payload = _json_loads(_row_value(row, "payload_json"), {})
    if isinstance(payload, dict) and payload:
        return payload

    event = _event_from_row(row)
    nested_payload = event.get("payload") if isinstance(event, dict) else {}
    return nested_payload if isinstance(nested_payload, dict) else {}


def _event_from_row(row: Any) -> dict[str, Any]:
    frame_blob = _row_value(row, "frame_blob")
    frame_format = str(_row_value(row, "frame_format") or "").strip()
    if frame_blob and frame_format == RUN_EVENT_FRAME_FORMAT:
        try:
            raw = bytes(frame_blob)
            event = _json_loads(zlib.decompress(raw).decode("utf-8"), {})
            if isinstance(event, dict):
                return event
        except Exception:
            pass
    event = _json_loads(_row_value(row, "event_json"), {})
    return event if isinstance(event, dict) else {}


__all__ = ["RUN_EVENT_FRAME_FORMAT", "payload_from_run_event_row"]
