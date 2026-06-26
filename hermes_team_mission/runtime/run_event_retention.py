"""Team Mission run-event retention policy hooks."""

from __future__ import annotations

import json
from typing import Any


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _truthy_payload_flag(payload: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _payload_has_stream_text(payload: dict[str, Any]) -> bool:
    for key in ("delta", "text", "snapshot"):
        if str(payload.get(key) or "").strip():
            return True
    return False


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _payload_from_row(row: Any) -> dict[str, Any]:
    event = _json_loads(_row_value(row, "event_json"), {})
    payload = event.get("payload") if isinstance(event, dict) else {}
    if isinstance(payload, dict):
        return payload
    payload = _json_loads(_row_value(row, "payload_json"), {})
    return payload if isinstance(payload, dict) else {}


def _run_event_stream_text_exists(
    conn: Any,
    *,
    session_id: str,
    run_id: str,
) -> bool:
    session_id = str(session_id or "").strip()
    run_id = str(run_id or "").strip()
    if not session_id or not run_id:
        return False
    rows = conn.execute(
        """
        SELECT payload_json, event_json
        FROM run_events
        WHERE session_id = ?
          AND run_id = ?
          AND event_type = 'message.delta'
        ORDER BY seq ASC, id ASC
        LIMIT 128
        """,
        (session_id, run_id),
    ).fetchall()
    for row in rows:
        if _payload_has_stream_text(_payload_from_row(row)):
            return True
    return False


def should_preserve_terminal_stream_row(conn: Any, row: Any) -> bool:
    """Return true when pruning this terminal stream row would lose Team Mission truth."""

    event_type = str(_row_value(row, "event_type") or "").strip()
    if event_type != "message.delta":
        return False
    payload = _payload_from_row(row)
    if not (
        _truthy_payload_flag(payload, "team_mission_final_deliverable", "teamMissionFinalDeliverable")
        and _truthy_payload_flag(payload, "team_mission_conversation_mirror", "teamMissionConversationMirror")
    ):
        return False
    if str(payload.get("mode") or "").strip().lower() != "snapshot":
        return True
    source_session_id = str(payload.get("source_session_id") or payload.get("sourceSessionId") or "").strip()
    source_run_id = str(payload.get("source_run_id") or payload.get("sourceRunId") or "").strip()
    return not _run_event_stream_text_exists(
        conn,
        session_id=source_session_id,
        run_id=source_run_id,
    )
