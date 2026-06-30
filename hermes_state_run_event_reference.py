from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from hermes_conversation_message_identity import AssistantMessageIdentity
from hermes_conversation_message_identity import assistant_conversation_message_id_for
from hermes_state_run_event_codec import decode_run_event_row
from hermes_state_run_event_codec import encode_run_event_frame
from hermes_state_run_event_index import (
    project_run_event_search_index,
    runtime_source_seq_from_event,
)


REFERENCE_SUMMARY_CHARS = 500
REFERENCE_VERSION = "2026-06-28"
REFERENCEABLE_EVENT_TYPES = ("message.complete", "tool.complete")
TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _message_seq_in_run(event: dict[str, Any]) -> str:
    payload = _record(event.get("payload"))
    seq = _text(
        event.get("message_seq_in_run")
        or event.get("messageSeqInRun")
        or payload.get("message_seq_in_run")
        or payload.get("messageSeqInRun")
    )
    if seq:
        return seq
    source_seq = _text(event.get("seq") or payload.get("seq"))
    return f"legacy-source-seq:{source_seq}" if source_seq else ""


def _message_text_from_payload(payload: dict[str, Any]) -> str:
    for key in ("text", "content", "output", "final_response", "finalResponse", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_call_id_from_payload(payload: dict[str, Any]) -> str:
    source = _record(payload.get("source_payload") or payload.get("sourcePayload"))
    for key in ("tool_call_id", "toolCallId", "tool_id", "toolId", "id"):
        value = _text(payload.get(key) or source.get(key))
        if value:
            return value
    return ""


def _message_row_for_event(conn: sqlite3.Connection, event: dict[str, Any]) -> sqlite3.Row | None:
    payload = _record(event.get("payload"))
    session_id = _text(
        event.get("stored_session_id")
        or event.get("storedSessionId")
        or payload.get("stored_session_id")
        or payload.get("storedSessionId")
        or event.get("session_id")
        or event.get("sessionId")
    )
    run_id = _text(event.get("run_id") or event.get("runId") or payload.get("run_id") or payload.get("runId"))
    message_seq = _message_seq_in_run(event)
    if not session_id or not run_id or not message_seq:
        return None
    conversation_message_id = _text(
        event.get("conversation_message_id")
        or event.get("conversationMessageId")
        or payload.get("conversation_message_id")
        or payload.get("conversationMessageId")
    ) or assistant_conversation_message_id_for(
        AssistantMessageIdentity(
            session_id=session_id,
            run_id=run_id,
            message_seq_in_run=message_seq,
        )
    )
    return conn.execute(
        """
        SELECT id, session_id, role, content, conversation_message_id, metadata_json, reasoning
        FROM messages
        WHERE session_id = ?
          AND conversation_message_id = ?
          AND active = 1
        ORDER BY id ASC
        LIMIT 1
        """,
        (session_id, conversation_message_id),
    ).fetchone()


def _tool_row_for_event(
    conn: sqlite3.Connection,
    row: Any,
    event: dict[str, Any],
) -> sqlite3.Row | None:
    payload = _record(event.get("payload"))
    session_id = _text(
        event.get("stored_session_id")
        or payload.get("stored_session_id")
        or _row_value(row, "session_id")
    )
    if not session_id:
        return None
    projected_id = _text(_row_value(row, "projected_tool_event_id"))
    if projected_id:
        found = conn.execute(
            """
            SELECT *
            FROM tool_events
            WHERE session_id = ?
              AND id = ?
            LIMIT 1
            """,
            (session_id, projected_id),
        ).fetchone()
        if found is not None:
            return found
    tool_call_id = _tool_call_id_from_payload(payload)
    if tool_call_id:
        found = conn.execute(
            """
            SELECT *
            FROM tool_events
            WHERE session_id = ?
              AND tool_call_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (session_id, tool_call_id),
        ).fetchone()
        if found is not None:
            return found
    return conn.execute(
        """
        SELECT *
        FROM tool_events
        WHERE session_id = ?
          AND (? = '' OR COALESCE(run_id, '') = ?)
          AND (? = '' OR COALESCE(turn_id, '') = ?)
          AND COALESCE(seq_last, 0) = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            session_id,
            _text(_row_value(row, "run_id")),
            _text(_row_value(row, "run_id")),
            _text(_row_value(row, "turn_id")),
            _text(_row_value(row, "turn_id")),
            int(_row_value(row, "seq", 0) or event.get("seq") or 0),
        ),
    ).fetchone()


def _referenced_message_event(event: dict[str, Any], message_row: sqlite3.Row) -> tuple[dict[str, Any], str]:
    payload = dict(_record(event.get("payload")))
    content = _string_value(message_row["content"])
    conversation_message_id = _text(message_row["conversation_message_id"])
    original_text = _message_text_from_payload(payload) or content
    reference = {
        "kind": "message",
        "table": "messages",
        "conversation_message_id": conversation_message_id,
        "content_sha256": _sha256_text(content),
        "content_length": len(content),
        "version": REFERENCE_VERSION,
    }
    slim_payload = {
        "run_id": _text(payload.get("run_id") or event.get("run_id")),
        "turn_id": _text(payload.get("turn_id") or event.get("turn_id")),
        "status": _text(payload.get("status")) or "complete",
        "role": _text(payload.get("role")) or "assistant",
        "summary": original_text[:REFERENCE_SUMMARY_CHARS],
        "message_seq_in_run": _message_seq_in_run(event),
        "referenced_payload": reference,
    }
    return {**event, "payload": slim_payload}, conversation_message_id


def _referenced_tool_event(event: dict[str, Any], tool_row: sqlite3.Row) -> tuple[dict[str, Any], str]:
    payload = dict(_record(event.get("payload")))
    result_text = _string_value(tool_row["result_text"])
    summary = _text(tool_row["summary"]) or result_text[:REFERENCE_SUMMARY_CHARS]
    result_json = _text(tool_row["result_json"])
    reference = {
        "kind": "tool",
        "table": "tool_events",
        "tool_event_id": int(tool_row["id"]),
        "tool_call_id": _text(tool_row["tool_call_id"]),
        "result_text_sha256": _sha256_text(result_text) if result_text else "",
        "result_text_length": len(result_text),
        "result_json_sha256": _sha256_text(result_json) if result_json else "",
        "result_json_length": len(result_json),
        "version": REFERENCE_VERSION,
    }
    slim_payload = {
        "tool_call_id": _text(tool_row["tool_call_id"]),
        "tool_id": _text(tool_row["tool_call_id"]),
        "name": _text(tool_row["tool_name"]) or _text(payload.get("name")),
        "tool_name": _text(tool_row["tool_name"]) or _text(payload.get("tool_name")),
        "status": _text(tool_row["status"]) or _text(payload.get("status")) or "completed",
        "summary": summary[:REFERENCE_SUMMARY_CHARS],
        "referenced_payload": reference,
    }
    return {**event, "payload": slim_payload}, str(int(tool_row["id"]))


def referenced_event_for_row(
    conn: sqlite3.Connection,
    row: Any,
    event: dict[str, Any],
) -> tuple[dict[str, Any], str, str] | None:
    event_type = _text(event.get("type") or _row_value(row, "event_type"))
    if event_type == "message.complete":
        message_row = _message_row_for_event(conn, event)
        if message_row is None:
            return None
        referenced, message_id = _referenced_message_event(event, message_row)
        return referenced, message_id, ""
    if event_type == "tool.complete":
        tool_row = _tool_row_for_event(conn, row, event)
        if tool_row is None:
            return None
        if _text(tool_row["status"]) not in TERMINAL_TOOL_STATUSES:
            return None
        referenced, tool_event_id = _referenced_tool_event(event, tool_row)
        return referenced, "", tool_event_id
    return None


def rehydrate_referenced_run_event(
    conn: sqlite3.Connection,
    row: Any,
    event: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(_record(event.get("payload")))
    reference = _record(payload.get("referenced_payload"))
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
                content = _string_value(message_row["content"])
                metadata = _record(_json_loads(message_row["metadata_json"], {}))
                payload.setdefault("text", content)
                payload.setdefault("content", content)
                if metadata.get("projection_status"):
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
                result = _json_loads(tool_row["result_json"], None)
                if result is not None:
                    payload.setdefault("result", result)
                result_text = _string_value(tool_row["result_text"])
                if result_text:
                    payload.setdefault("result_text", result_text)
                payload.setdefault("status", _text(tool_row["status"]))
                payload.setdefault("tool_call_id", _text(tool_row["tool_call_id"]))
                payload.setdefault("tool_id", _text(tool_row["tool_call_id"]))
                payload.setdefault("tool_name", _text(tool_row["tool_name"]))
                payload.setdefault("name", _text(tool_row["tool_name"]))
    if payload is event.get("payload"):
        return event
    return {**event, "payload": payload}


def reference_projected_run_event_payloads(
    conn: sqlite3.Connection,
    *,
    session_id: str = "",
    limit: int = 1000,
) -> dict[str, int]:
    stable_filter = _text(session_id)
    bounded_limit = max(1, min(int(limit or 1000), 20000))
    params: list[Any] = [*REFERENCEABLE_EVENT_TYPES]
    session_clause = ""
    if stable_filter:
        session_clause = "AND e.session_id = ?"
        params.append(stable_filter)
    placeholders = ",".join("?" for _ in REFERENCEABLE_EVENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT e.*
        FROM run_events e
        LEFT JOIN runs r ON r.run_id = e.run_id
        WHERE e.event_type IN ({placeholders})
          AND COALESCE(e.projection_state, '') != 'referenced'
          AND COALESCE(r.status, 'completed') NOT IN (
              'queued', 'starting', 'running', 'waiting_approval', 'cancelling', 'finalizing'
          )
          {session_clause}
        ORDER BY e.session_id ASC, e.seq ASC, e.id ASC
        LIMIT ?
        """,
        (*params, bounded_limit),
    ).fetchall()
    referenced_events = 0
    skipped_events = 0
    for row in rows:
        event = decode_run_event_row(row)
        referenced = referenced_event_for_row(conn, row, event)
        if referenced is None:
            skipped_events += 1
            continue
        referenced_event, projected_message_id, projected_tool_event_id = referenced
        payload = _record(referenced_event.get("payload"))
        frame_blob, frame_format = encode_run_event_frame(referenced_event)
        runtime_source_seq = runtime_source_seq_from_event(referenced_event)
        conn.execute(
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
                _json_dumps(payload),
                _json_dumps(referenced_event),
                frame_blob,
                frame_format,
                projected_message_id,
                projected_tool_event_id,
                runtime_source_seq,
                int(row["id"]),
            ),
        )
        project_run_event_search_index(
            conn,
            row_id=int(row["id"]),
            session_id=_text(row["session_id"]),
            seq=int(row["seq"] or referenced_event.get("seq") or 0),
            event_type=_text(row["event_type"] or referenced_event.get("type")),
            runtime_scope_key=_text(row["runtime_scope_key"] or referenced_event.get("runtime_scope_key")),
            runtime_source_seq=runtime_source_seq,
            event=referenced_event,
            updated_at=float(row["timestamp"] or referenced_event.get("timestamp") or 0),
        )
        referenced_events += 1
    remaining_row = conn.execute(
        f"""
        SELECT COUNT(1) AS count
        FROM run_events e
        LEFT JOIN runs r ON r.run_id = e.run_id
        WHERE e.event_type IN ({placeholders})
          AND COALESCE(e.projection_state, '') != 'referenced'
          AND COALESCE(r.status, 'completed') NOT IN (
              'queued', 'starting', 'running', 'waiting_approval', 'cancelling', 'finalizing'
          )
          {session_clause}
        """,
        tuple(params),
    ).fetchone()
    return {
        "referenced_events": referenced_events,
        "skipped_events": skipped_events,
        "remaining_events": int(_row_value(remaining_row, "count", 0) or 0),
        "limit": bounded_limit,
    }
