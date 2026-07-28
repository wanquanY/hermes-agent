"""Read model for one Team Mission node's durable transcript."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_agent.repositories.message_content_codec import decode_message_content
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_team_mission.domain.transcript_visibility import (
    is_node_transcript_message,
)


class TeamMissionNodeHistoryReadModel:
    """Projects persisted messages into the node-history gateway contract."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)

    def list_messages(
        self,
        session_id: str,
        *,
        limit: int = 50,
        activity_id: str = "",
        node_id: str = "",
        after_id: int = 0,
        before_id: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conversation_session_id = _text(session_id)
        if not conversation_session_id:
            return [], 0
        bounded_limit = max(1, min(int(limit or 50), 500))
        normalized_after_id = max(0, int(after_id or 0))
        normalized_before_id = max(0, int(before_id or 0))
        stable_activity_id = _text(activity_id)
        stable_node_id = _text(node_id)

        with self._lock:
            if stable_activity_id:
                return self._list_activity_messages(
                    conversation_session_id,
                    activity_id=stable_activity_id,
                    node_id=stable_node_id,
                    limit=bounded_limit,
                    after_id=normalized_after_id,
                    before_id=normalized_before_id,
                )
            total_row = self._conn.execute(
                "SELECT count(*) AS count FROM messages "
                "WHERE session_id = ? AND active = 1",
                (conversation_session_id,),
            ).fetchone()
            rows = self._list_session_rows(
                conversation_session_id,
                limit=bounded_limit,
                after_id=normalized_after_id,
                before_id=normalized_before_id,
            )
        total = int(_row_value(total_row, "count", len(rows)) or len(rows))
        return [_message_from_row(row) for row in rows], total

    def _list_activity_messages(
        self,
        session_id: str,
        *,
        activity_id: str,
        node_id: str,
        limit: int,
        after_id: int,
        before_id: int,
    ) -> tuple[list[dict[str, Any]], int]:
        cursor_clause = ""
        cursor_params: list[Any] = []
        if after_id > 0:
            cursor_clause = "AND id > ?"
            cursor_params.append(after_id)
        elif before_id > 0:
            cursor_clause = "AND id < ?"
            cursor_params.append(before_id)
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM messages
            WHERE session_id = ?
              AND active = 1
              {cursor_clause}
              AND metadata_json LIKE ?
            ORDER BY id ASC
            """,
            (session_id, *cursor_params, f"%{activity_id}%"),
        ).fetchall()
        filtered = [
            message
            for message in (_message_from_row(row) for row in rows)
            if _matches_node_filter(
                message,
                activity_id=activity_id,
                node_id=node_id,
            )
        ]
        if after_id > 0:
            return filtered[:limit], len(filtered)
        return filtered[-limit:], len(filtered)

    def _list_session_rows(
        self,
        session_id: str,
        *,
        limit: int,
        after_id: int,
        before_id: int,
    ) -> list[Any]:
        if after_id > 0:
            return self._conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND active = 1 AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        if before_id > 0:
            return self._conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE session_id = ? AND active = 1 AND id < ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (session_id, before_id, limit),
            ).fetchall()
        return self._conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE session_id = ? AND active = 1
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (session_id, limit),
        ).fetchall()


def _message_from_row(row: Any) -> dict[str, Any]:
    content = decode_message_content(_row_value(row, "content", ""))
    role = _text(_row_value(row, "role", "assistant"))
    if role not in {"assistant", "system", "tool", "user"}:
        role = "assistant"
    metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
    row_id = int(_row_value(row, "id", 0) or 0)
    message: dict[str, Any] = {
        "id": row_id,
        "role": role,
        "message_id": _text(_row_value(row, "platform_message_id", ""))
        or str(row_id),
        "timestamp": float(_row_value(row, "timestamp", 0) or 0),
        "text": str(content or ""),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    for key in ("conversation_message_id", "participant_id", "tool_call_id"):
        value = _text(_row_value(row, key, ""))
        if value:
            message[key] = value
    tool_calls = _row_value(row, "tool_calls", "")
    if tool_calls:
        parsed_tool_calls = _json_loads(tool_calls, [])
        if isinstance(parsed_tool_calls, list):
            message["tool_calls"] = parsed_tool_calls
    reasoning = _text(
        _row_value(row, "reasoning", "")
        or _row_value(row, "reasoning_content", "")
        or _row_value(row, "reasoning_details", "")
    )
    if reasoning:
        message["reasoning"] = reasoning
    if role == "tool":
        name = _text(_row_value(row, "tool_name", ""))
        if name:
            message["name"] = name
        if message["text"]:
            message["result_text"] = message["text"]
    return message


def _matches_node_filter(
    message: dict[str, Any],
    *,
    activity_id: str,
    node_id: str,
) -> bool:
    if not is_node_transcript_message(message):
        return False
    metadata = _mapping(message.get("metadata"))
    if activity_id and _metadata_activity_id(metadata) != activity_id:
        return False
    message_node_id = _metadata_node_id(metadata)
    return not node_id or not message_node_id or message_node_id == node_id


def _metadata_activity_id(metadata: dict[str, Any]) -> str:
    run_context = _mapping(metadata.get("run_context"))
    return _text(
        metadata.get("activity_id")
        or metadata.get("activityId")
        or run_context.get("activity_id")
        or run_context.get("activityId")
    )


def _metadata_node_id(metadata: dict[str, Any]) -> str:
    team_mission = _mapping(metadata.get("team_mission"))
    return _text(
        metadata.get("node_id")
        or metadata.get("nodeId")
        or team_mission.get("node_id")
        or team_mission.get("nodeId")
    )


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return fallback
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return fallback if parsed is None else parsed


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["TeamMissionNodeHistoryReadModel"]
