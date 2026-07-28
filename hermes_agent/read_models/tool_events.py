from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from typing import Any

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.domain.run_event_codec import decode_run_event_row
from hermes_agent.domain.tool_lifecycle import TERMINAL_TOOL_STATUSES
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


TOOL_EVENT_TYPES = frozenset(
    {
        "tool.generating",
        "tool.start",
        "tool.progress",
        "tool.complete",
    }
)
PROJECTION_VERSION = "2026-06-28"


class ToolEventProjectionReadModel:
    """Read-only access to the derived ``tool_events`` projection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)

    def list(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        run_id: str = "",
        direction: str = "after",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        stable = str(session_id or "").strip()
        if not stable:
            return []
        with self._lock:
            rows = EventLedger(self._conn).list_tool_event_projection_rows(
                stable,
                after_seq=int(after_seq or 0),
                run_id=run_id,
                direction=direction,
                limit=limit,
            )
        return [
            item
            for row in rows
            if (item := tool_event_row_to_dict(row))
        ]


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    direct = _record(payload.get("source_payload") or payload.get("sourcePayload"))
    if direct:
        return direct
    source_event = _record(
        payload.get("source_event")
        or payload.get("sourceEvent")
        or payload.get("runtime_event")
        or payload.get("runtimeEvent")
    )
    return _record(source_event.get("payload"))


def _session_id(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return _first_text(
        event.get("conversation_session_id"),
        event.get("conversationSessionId"),
        payload.get("conversation_session_id"),
        payload.get("conversationSessionId"),
        event.get("session_id"),
        event.get("sessionId"),
    )


def _run_id(event: dict[str, Any]) -> str:
    payload = _payload(event)
    origin = _record(payload.get("origin"))
    return _first_text(
        event.get("run_id"),
        event.get("runId"),
        payload.get("run_id"),
        payload.get("runId"),
        origin.get("run_id"),
        origin.get("runId"),
    )


def _turn_id(event: dict[str, Any]) -> str:
    payload = _payload(event)
    origin = _record(payload.get("origin"))
    return _first_text(
        event.get("turn_id"),
        event.get("turnId"),
        payload.get("turn_id"),
        payload.get("turnId"),
        origin.get("turn_id"),
        origin.get("turnId"),
    )


def _participant_id(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return _first_text(
        event.get("participant_id"),
        event.get("participantId"),
        payload.get("participant_id"),
        payload.get("participantId"),
    )


def _tool_name(payload: dict[str, Any]) -> str:
    source = _source_payload(payload)
    source_tool = _record(source.get("tool"))
    source_function = _record(source.get("function") or source_tool.get("function"))
    return _first_text(
        payload.get("name"),
        payload.get("tool"),
        payload.get("tool_name"),
        payload.get("toolName"),
        source.get("name"),
        source.get("tool"),
        source.get("tool_name"),
        source.get("toolName"),
        source_tool.get("name"),
        source_function.get("name"),
    ) or "tool"


def _explicit_tool_call_id(payload: dict[str, Any]) -> str:
    source = _source_payload(payload)
    return _first_text(
        payload.get("tool_call_id"),
        payload.get("toolCallId"),
        payload.get("tool_id"),
        payload.get("toolId"),
        payload.get("id"),
        source.get("tool_call_id"),
        source.get("toolCallId"),
        source.get("tool_id"),
        source.get("toolId"),
        source.get("id"),
    )


def _client_message_id(payload: dict[str, Any]) -> str:
    origin = _record(payload.get("origin"))
    return _first_text(
        payload.get("client_message_id"),
        payload.get("clientMessageId"),
        origin.get("client_message_id"),
        origin.get("clientMessageId"),
    )


def _argument_value(payload: dict[str, Any]) -> Any:
    source = _source_payload(payload)
    for key in ("arguments", "args", "input", "parameters"):
        if key in payload:
            return copy.deepcopy(payload.get(key))
        if key in source:
            return copy.deepcopy(source.get(key))
    return None


def _json_value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json_dumps_compact(value)


def _result_value(payload: dict[str, Any]) -> Any:
    source = _source_payload(payload)
    for key in ("result", "output", "data"):
        if key in payload:
            return copy.deepcopy(payload.get(key))
        if key in source:
            return copy.deepcopy(source.get(key))
    return None


def _result_text(payload: dict[str, Any], result: Any) -> str:
    source = _source_payload(payload)
    for key in ("result_text", "resultText", "output_text", "output", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return result if isinstance(result, str) else ""


def _summary(payload: dict[str, Any], result_text: str) -> str:
    summary = _first_text(payload.get("summary"), _source_payload(payload).get("summary"))
    if summary:
        return summary
    return result_text[:500] if result_text else ""


def _duration_s(payload: dict[str, Any]) -> float | None:
    value = payload.get("duration_s", payload.get("duration"))
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _status(event_type: str, payload: dict[str, Any]) -> str:
    raw = _text(payload.get("status")).lower()
    if event_type == "tool.complete":
        if raw in {"failed", "error"} or payload.get("error") or payload.get("is_error"):
            return "failed"
        if raw in {"cancelled", "canceled"}:
            return "cancelled"
        if raw == "interrupted":
            return "interrupted"
        return "completed"
    if raw in TERMINAL_TOOL_STATUSES:
        return raw
    if event_type == "tool.generating":
        return "generating"
    return "running"


def _progress_json(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "tool.complete":
        return ""
    allowed = (
        "preview",
        "message",
        "text",
        "delta",
        "status",
        "context",
        "duration_s",
        "duration",
    )
    progress = {key: copy.deepcopy(payload[key]) for key in allowed if key in payload}
    progress["source_event_type"] = event_type
    return json_dumps_compact(progress) if progress else ""


def _metadata_json(event: dict[str, Any], explicit_tool_call_id: str) -> str:
    payload = _payload(event)
    metadata: dict[str, Any] = {
        "projection_version": PROJECTION_VERSION,
        "source_event_type": _text(event.get("type")),
    }
    for key, value in {
        "execution_session_id": event.get("execution_session_id") or event.get("session_id"),
        "runtime_scope_key": event.get("runtime_scope_key"),
        "client_message_id": _client_message_id(payload),
        "context": payload.get("context"),
        "duration_s": _duration_s(payload),
        "tool_call_id_explicit": bool(explicit_tool_call_id),
    }.items():
        if value not in (None, ""):
            metadata[key] = value
    return json_dumps_compact(metadata)


def _synthetic_tool_call_id(
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    tool_name: str,
    event_type: str,
    seq: int,
) -> str:
    raw = f"{session_id}\0{run_id}\0{turn_id}\0{tool_name}\0{event_type}\0{seq}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"synthetic:{digest}"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _find_open_tool_call_id(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    tool_name: str,
) -> str:
    clauses = [
        "session_id = ?",
        "tool_name = ?",
        "status IN ('generating', 'running')",
    ]
    params: list[Any] = [session_id, tool_name]
    if run_id:
        clauses.append("COALESCE(run_id, '') = ?")
        params.append(run_id)
    if turn_id:
        clauses.append("COALESCE(turn_id, '') = ?")
        params.append(turn_id)
    row = conn.execute(
        f"""
        SELECT tool_call_id
        FROM tool_events
        WHERE {" AND ".join(clauses)}
        ORDER BY COALESCE(seq_last, seq_start, 0) DESC, id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return _text(_row_value(row, "tool_call_id"))


def _promote_synthetic_tool_id(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    run_id: str,
    turn_id: str,
    tool_name: str,
    explicit_tool_call_id: str,
) -> None:
    if not explicit_tool_call_id:
        return
    existing = conn.execute(
        """
        SELECT 1
        FROM tool_events
        WHERE session_id = ? AND tool_call_id = ?
        LIMIT 1
        """,
        (session_id, explicit_tool_call_id),
    ).fetchone()
    if existing is not None:
        return
    clauses = [
        "session_id = ?",
        "tool_name = ?",
        "tool_call_id LIKE 'synthetic:%'",
        "status IN ('generating', 'running')",
    ]
    params: list[Any] = [session_id, tool_name]
    if run_id:
        clauses.append("COALESCE(run_id, '') = ?")
        params.append(run_id)
    if turn_id:
        clauses.append("COALESCE(turn_id, '') = ?")
        params.append(turn_id)
    row = conn.execute(
        f"""
        SELECT id
        FROM tool_events
        WHERE {" AND ".join(clauses)}
        ORDER BY COALESCE(seq_last, seq_start, 0) DESC, id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    row_id = _row_value(row, "id")
    if row_id is None:
        return
    conn.execute(
        "UPDATE tool_events SET tool_call_id = ? WHERE id = ?",
        (explicit_tool_call_id, row_id),
    )


def project_tool_event(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    event_type = _text(event.get("type"))
    if event_type not in TOOL_EVENT_TYPES:
        return None
    session_id = _session_id(event)
    if not session_id:
        return None
    payload = _payload(event)
    run_id = _run_id(event)
    turn_id = _turn_id(event)
    tool_name = _tool_name(payload)
    try:
        seq = int(event.get("seq") or payload.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    timestamp = float(event.get("timestamp") or payload.get("timestamp") or 0)
    explicit_tool_call_id = _explicit_tool_call_id(payload)
    if explicit_tool_call_id and event_type == "tool.start":
        _promote_synthetic_tool_id(
            conn,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_name=tool_name,
            explicit_tool_call_id=explicit_tool_call_id,
        )
    tool_call_id = explicit_tool_call_id or _find_open_tool_call_id(
        conn,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        tool_name=tool_name,
    )
    if not tool_call_id:
        tool_call_id = _synthetic_tool_call_id(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_name=tool_name,
            event_type=event_type,
            seq=seq,
        )
    arguments = _argument_value(payload)
    result = _result_value(payload) if event_type == "tool.complete" else None
    result_text = _result_text(payload, result) if event_type == "tool.complete" else ""
    status = _status(event_type, payload)
    progress_json = _progress_json(event_type, payload)
    metadata_json = _metadata_json(event, explicit_tool_call_id)
    started_at = timestamp if event_type in {"tool.generating", "tool.start"} else None
    completed_at = timestamp if status in TERMINAL_TOOL_STATUSES else None

    conn.execute(
        """
        INSERT INTO tool_events (
            session_id, run_id, turn_id, tool_call_id, tool_name, status,
            started_at, updated_at, completed_at, seq_start, seq_last,
            arguments_json, progress_json, result_json, result_text,
            summary, participant_id, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, tool_call_id) DO UPDATE SET
            run_id = COALESCE(NULLIF(excluded.run_id, ''), tool_events.run_id),
            turn_id = COALESCE(NULLIF(excluded.turn_id, ''), tool_events.turn_id),
            tool_name = COALESCE(NULLIF(excluded.tool_name, ''), tool_events.tool_name),
            status = CASE
                WHEN tool_events.status IN ('completed', 'failed', 'cancelled', 'interrupted')
                  AND excluded.status IN ('generating', 'running')
                THEN tool_events.status
                ELSE excluded.status
            END,
            started_at = COALESCE(tool_events.started_at, excluded.started_at),
            updated_at = excluded.updated_at,
            completed_at = COALESCE(excluded.completed_at, tool_events.completed_at),
            seq_start = CASE
                WHEN COALESCE(tool_events.seq_start, 0) = 0 THEN excluded.seq_start
                WHEN COALESCE(excluded.seq_start, 0) = 0 THEN tool_events.seq_start
                WHEN excluded.seq_start < tool_events.seq_start THEN excluded.seq_start
                ELSE tool_events.seq_start
            END,
            seq_last = MAX(COALESCE(tool_events.seq_last, 0), COALESCE(excluded.seq_last, 0)),
            arguments_json = COALESCE(NULLIF(excluded.arguments_json, ''), tool_events.arguments_json),
            progress_json = COALESCE(NULLIF(excluded.progress_json, ''), tool_events.progress_json),
            result_json = COALESCE(NULLIF(excluded.result_json, ''), tool_events.result_json),
            result_text = COALESCE(NULLIF(excluded.result_text, ''), tool_events.result_text),
            summary = COALESCE(NULLIF(excluded.summary, ''), tool_events.summary),
            participant_id = COALESCE(NULLIF(excluded.participant_id, ''), tool_events.participant_id),
            metadata_json = excluded.metadata_json
        """,
        (
            session_id,
            run_id,
            turn_id,
            tool_call_id,
            tool_name,
            status,
            started_at,
            timestamp,
            completed_at,
            seq,
            seq,
            json_dumps_compact(arguments) if arguments is not None else "",
            progress_json,
            json_dumps_compact(result) if result is not None else "",
            result_text,
            _summary(payload, result_text),
            _participant_id(event),
            metadata_json,
        ),
    )
    row = conn.execute(
        """
        SELECT *
        FROM tool_events
        WHERE session_id = ? AND tool_call_id = ?
        LIMIT 1
        """,
        (session_id, tool_call_id),
    ).fetchone()
    return tool_event_row_to_dict(row)


def backfill_tool_events_from_run_events(
    conn: sqlite3.Connection,
    *,
    logger: Any = None,
) -> None:
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM run_events
            WHERE event_type IN (
                'tool.generating',
                'tool.start',
                'tool.progress',
                'tool.complete'
            )
            ORDER BY session_id ASC, seq ASC, id ASC
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if logger is not None:
            logger.debug("tool_events backfill skipped: %s", exc)
        return

    for row in rows:
        event = decode_run_event_row(row)
        event = event if isinstance(event, dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else None
        payload = payload if isinstance(payload, dict) else {}
        if not event:
            event = {
                "type": _row_value(row, "event_type"),
                "payload": payload,
            }
        event.setdefault("type", _row_value(row, "event_type"))
        event.setdefault("conversation_session_id", _row_value(row, "session_id"))
        event.setdefault("run_id", _row_value(row, "run_id"))
        event.setdefault("turn_id", _row_value(row, "turn_id"))
        event.setdefault("execution_session_id", _row_value(row, "execution_session_id"))
        event.setdefault("runtime_scope_key", _row_value(row, "runtime_scope_key"))
        event.setdefault("participant_id", _row_value(row, "participant_id"))
        event.setdefault("seq", _row_value(row, "seq"))
        event.setdefault("timestamp", _row_value(row, "timestamp"))
        try:
            project_tool_event(conn, event)
        except Exception as exc:
            if logger is not None:
                logger.debug(
                    "tool_events backfill skipped row %s/%s: %s",
                    _row_value(row, "session_id"),
                    _row_value(row, "seq"),
                    exc,
                )


def tool_event_row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    arguments = json_loads(_row_value(row, "arguments_json"), None)
    progress = json_loads(_row_value(row, "progress_json"), None)
    result = json_loads(_row_value(row, "result_json"), None)
    metadata = json_loads(_row_value(row, "metadata_json"), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    status = _text(_row_value(row, "status"))
    event_type = "tool.complete" if status in TERMINAL_TOOL_STATUSES else "tool.start"
    tool_call_id = _text(_row_value(row, "tool_call_id"))
    item = {
        "id": int(_row_value(row, "id", 0) or 0),
        "type": event_type,
        "session_id": _text(_row_value(row, "session_id")),
        "conversation_session_id": _text(_row_value(row, "session_id")),
        "run_id": _text(_row_value(row, "run_id")),
        "turn_id": _text(_row_value(row, "turn_id")),
        "tool_call_id": tool_call_id,
        "toolCallId": tool_call_id,
        "tool_id": tool_call_id,
        "toolId": tool_call_id,
        "tool_name": _text(_row_value(row, "tool_name")),
        "name": _text(_row_value(row, "tool_name")),
        "status": status,
        "phase": "complete" if status in TERMINAL_TOOL_STATUSES else "running",
        "started_at": float(_row_value(row, "started_at", 0) or 0),
        "startedAt": float(_row_value(row, "started_at", 0) or 0),
        "updated_at": float(_row_value(row, "updated_at", 0) or 0),
        "updatedAt": float(_row_value(row, "updated_at", 0) or 0),
        "completed_at": float(_row_value(row, "completed_at", 0) or 0),
        "completedAt": float(_row_value(row, "completed_at", 0) or 0),
        "seq_start": int(_row_value(row, "seq_start", 0) or 0),
        "seqStart": int(_row_value(row, "seq_start", 0) or 0),
        "seq_last": int(_row_value(row, "seq_last", 0) or 0),
        "seqLast": int(_row_value(row, "seq_last", 0) or 0),
        "participant_id": _text(_row_value(row, "participant_id")),
        "participantId": _text(_row_value(row, "participant_id")),
        "summary": _text(_row_value(row, "summary")),
        "result_text": _json_value_text(_row_value(row, "result_text")),
        "resultText": _json_value_text(_row_value(row, "result_text")),
        "metadata": metadata,
    }
    if arguments is not None:
        item["arguments"] = arguments
    if progress is not None:
        item["progress"] = progress
    if result is not None:
        item["result"] = result
    payload = {
        "tool_call_id": tool_call_id,
        "tool_id": tool_call_id,
        "name": item["name"],
        "tool_name": item["tool_name"],
        "status": status,
        "summary": item["summary"],
    }
    if arguments is not None:
        payload["arguments"] = arguments
    if result is not None:
        payload["result"] = result
    if item["result_text"]:
        payload["result_text"] = item["result_text"]
    item["payload"] = payload
    return item


# Canonical tool-event reader (PR-3 §4.3) -----------------------------------
#
# The ``run_events`` table is the authoritative event source: every
# ``tool.start`` / ``tool.complete`` (and ``tool.generating`` /
# ``tool.progress``) frame is persisted there with a real ``seq`` assigned by
# ``append_run_event``.  The ``tool_events`` table is a *projection* (read
# model) whose ``seq_start`` / ``seq_last`` columns are derived from — but
# not identical to — the canonical run_events ``seq`` values.
#
# Problem (triage S5/S7): when snapshot/pagination responses served the
# ``tool_events`` row model, FE code synthesized ``tool.start`` /
# ``tool.complete`` events and stamped them with ``seq_start``.  Those
# synthetic seqs did **not** match the canonical run_events seqs, so shared-seq
# deduplication in the FE ledger broke ("dual seq identity").
#
# Solution: serve canonical event shapes directly.  This function reads the
# ``run_events`` rows for the tool-event types and decodes each via
# ``decode_run_event_row``, producing the same dict shape that
# ``list_run_events`` returns — including the *real* ``run_events.seq``.
# FE no longer needs to reverse-derive events from the row model.
#
# ``tool_events`` table is intentionally untouched: it remains the read model
# and backfill source for legacy callers.


def _run_event_row_to_canonical(row: Any) -> dict[str, Any] | None:
    """Decode a ``run_events`` row into a canonical event dict.

    Mirrors the canonical run-event decode path minus the
    reference-rehydration step (which only affects cross-event references and
    is not needed for the tool-event snapshot/pagination use case).  Returns
    ``None`` when the row cannot be decoded to a dict.
    """
    event = decode_run_event_row(row)
    if not isinstance(event, dict) or not event:
        return None
    # ``decode_run_event_row`` already populates the canonical run-event
    # identity, ordering, participant, and payload fields required here.
    event["seq"] = int(event.get("seq") or _row_value(row, "seq", 0) or 0)
    return event


def list_tool_events_as_canonical(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_seq: int = 0,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return canonical tool events (``tool.start``/``tool.complete``/…) for a session.

    Reads directly from the ``run_events`` table so each returned event carries
    its *real* ``run_events.seq`` — not the ``tool_events.seq_start`` projection
    value.  The returned dicts have the same shape as ``list_run_events``
    output: ``{type, seq, run_id, turn_id, payload, conversation_session_id, ...}``.

    Parameters mirror ``list_run_events`` for cursor compatibility:
    ``after_seq`` is an exclusive forward cursor (only events with ``seq >
    after_seq`` are returned).

    The ``tool_events`` projection table is **not** queried here; it remains a
    read model / backfill source for legacy callers.
    """
    stable = str(session_id or "").strip()
    if not stable:
        return []
    bounded_limit = max(1, min(int(limit or 2000), 5000))
    cursor_seq = int(after_seq or 0)
    type_placeholders = ", ".join("?" for _ in TOOL_EVENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT *
        FROM run_events
        WHERE session_id = ?
          AND seq > ?
          AND event_type IN ({type_placeholders})
        ORDER BY seq ASC
        LIMIT ?
        """,
        (stable, cursor_seq, *TOOL_EVENT_TYPES, bounded_limit),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        event = _run_event_row_to_canonical(row)
        if event is not None:
            events.append(event)
    return events
