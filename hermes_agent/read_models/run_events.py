"""Read model for canonical run event replay."""

from __future__ import annotations

import json
import sqlite3
import zlib
from typing import Any

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection

_RUN_EVENT_FRAME_FORMAT = "zlib+json:v1"
_STORED_SESSION_KEY = "stored" "_session_id"
_RUNTIME_SESSION_KEY = "runtime" "_session_id"


class RunEventReadModel:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)

    def list_runtime(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        active_only: bool = False,
        active_statuses: tuple[str, ...] = (),
        runtime_scope_key: str = "",
        run_id: str = "",
        activity_id: str = "",
        limit: int = 2000,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        stable = str(session_id or "").strip()
        if not stable:
            return []
        with self._lock:
            try:
                rows = EventLedger(self._conn).list_runtime_rows(
                    stable,
                    after_seq=int(after_seq or 0),
                    active_only=active_only,
                    active_statuses=active_statuses,
                    runtime_scope_key=runtime_scope_key,
                    run_id=run_id,
                    activity_id=activity_id,
                    limit=limit,
                    include_internal=include_internal,
                )
            except sqlite3.OperationalError as exc:
                if "no such table: run_events" in str(exc):
                    return []
                raise
            return self._decode_rows(rows)

    def list_filtered(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        runtime_scope_key: str = "",
        event_type_prefix: str = "",
        event_types: tuple[str, ...] | list[str] | None = None,
        payload_contains: str = "",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        stable = str(session_id or "").strip()
        if not stable:
            return []
        with self._lock:
            try:
                rows = EventLedger(self._conn).list_filtered_rows(
                    stable,
                    after_seq=int(after_seq or 0),
                    runtime_scope_key=runtime_scope_key,
                    event_types=event_types or (),
                    event_type_prefix=event_type_prefix,
                    payload_contains=payload_contains,
                    limit=limit,
                )
            except sqlite3.OperationalError as exc:
                if "no such table: run_events" in str(exc):
                    return []
                raise
            return self._decode_rows(rows)

    def _decode_rows(self, rows: list[Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in rows:
            event = _decode_run_event_row(row)
            if not isinstance(event, dict):
                continue
            event = _rehydrate_referenced_event(self._conn, row, event)
            event = _event_with_participant_id(
                event,
                str(_row_value(row, "participant_id", "") or ""),
            )
            activity_id = str(_row_value(row, "activity_id", "") or "").strip()
            if activity_id:
                event["activity_id"] = activity_id
                event["activityId"] = activity_id
                payload = event.get("payload")
                if isinstance(payload, dict):
                    payload = dict(payload)
                    payload.setdefault("activity_id", activity_id)
                    event["payload"] = payload
            events.append(event)
        return events


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _decode_run_event_row(row: Any) -> dict[str, Any]:
    event = _decode_run_event_frame_blob(
        _row_value(row, "frame_blob"),
        _row_value(row, "frame_format"),
    )
    if not event:
        event = _json_or(_row_value(row, "event_json"), {})
    event = event if isinstance(event, dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else None
    if payload is None:
        payload = _json_or(_row_value(row, "payload_json"), {})
    payload = payload if isinstance(payload, dict) else {}
    event.setdefault("type", _row_value(row, "event_type", ""))
    event.setdefault(_STORED_SESSION_KEY, _row_value(row, "session_id", ""))
    event.setdefault("session_id", _row_value(row, _RUNTIME_SESSION_KEY, ""))
    event.setdefault(_RUNTIME_SESSION_KEY, _row_value(row, _RUNTIME_SESSION_KEY, ""))
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


def _decode_run_event_frame_blob(blob: Any, frame_format: Any) -> dict[str, Any]:
    if not blob or _text(frame_format) != _RUN_EVENT_FRAME_FORMAT:
        return {}
    try:
        raw = bytes(blob)
        decoded = zlib.decompress(raw).decode("utf-8")
    except Exception:
        return {}
    event = _json_or(decoded, {})
    return event if isinstance(event, dict) else {}


def _rehydrate_referenced_event(
    conn: sqlite3.Connection,
    row: Any,
    event: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(event.get("payload")) if isinstance(event.get("payload"), dict) else {}
    reference = payload.get("referenced_payload") if isinstance(payload.get("referenced_payload"), dict) else {}
    kind = _text(reference.get("kind"))
    if kind == "message":
        conversation_message_id = _text(
            reference.get("conversation_message_id") or _row_value(row, "projected_message_id")
        )
        if conversation_message_id:
            message_row = conn.execute(
                """
                SELECT content, metadata_json
                FROM messages
                WHERE session_id = ?
                  AND conversation_message_id = ?
                  AND active = 1
                ORDER BY id ASC
                LIMIT 1
                """,
                (_text(_row_value(row, "session_id")), conversation_message_id),
            ).fetchone()
            if message_row is not None:
                content = _text(_row_value(message_row, "content", ""))
                metadata = _json_or(_row_value(message_row, "metadata_json"), {})
                payload.setdefault("text", content)
                payload.setdefault("content", content)
                if isinstance(metadata, dict) and metadata.get("projection_status"):
                    payload.setdefault("status", metadata.get("projection_status"))
    elif kind == "tool":
        tool_event_id = _text(reference.get("tool_event_id") or _row_value(row, "projected_tool_event_id"))
        if tool_event_id:
            tool_row = conn.execute(
                """
                SELECT *
                FROM tool_events
                WHERE session_id = ?
                  AND id = ?
                LIMIT 1
                """,
                (_text(_row_value(row, "session_id")), tool_event_id),
            ).fetchone()
            if tool_row is not None:
                result = _json_or(_row_value(tool_row, "result_json"), None)
                if result is not None:
                    payload.setdefault("result", result)
                result_text = _text(_row_value(tool_row, "result_text", ""))
                if result_text:
                    payload.setdefault("result_text", result_text)
                payload.setdefault("status", _text(_row_value(tool_row, "status", "")))
                tool_call_id = _text(_row_value(tool_row, "tool_call_id", ""))
                payload.setdefault("tool_call_id", tool_call_id)
                payload.setdefault("tool_id", tool_call_id)
                tool_name = _text(_row_value(tool_row, "tool_name", ""))
                payload.setdefault("tool_name", tool_name)
                payload.setdefault("name", tool_name)
    if payload == event.get("payload"):
        return event
    return {**event, "payload": payload}


def _event_with_participant_id(event: dict[str, Any], participant_id: str) -> dict[str, Any]:
    normalized = str(participant_id or "").strip()
    if not normalized:
        return event
    out = dict(event)
    out.setdefault("participant_id", normalized)
    payload = out.get("payload")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("participant_id", normalized)
        out["payload"] = payload
    return out


def _json_or(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["RunEventReadModel"]
