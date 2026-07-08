"""Read model for visible conversation message history pages.

This module owns the read-only transcript page projection used by gateway
history hydration. It replaces the legacy state facade for ordinary message
pages while keeping write/merge responsibilities out of the read model.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from agent.memory_manager import sanitize_context
from hermes_agent.repositories.message_repo import _decode_content as _decode_stored_content
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


@dataclass(frozen=True)
class MessagePageQuery:
    direction: str = "tail"
    cursor_id: int | None = None
    limit: int = 50
    include_ancestors: bool = False
    include_inactive: bool = False


class MessageHistoryReadModel:
    """SQLite-backed read model for paged conversation transcript messages."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)

    def page_as_conversation(self, session_id: str, query: MessagePageQuery) -> dict[str, Any]:
        with self._lock:
            return self._page_as_conversation_locked(session_id, query)

    def _page_as_conversation_locked(self, session_id: str, query: MessagePageQuery) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        if not stable:
            return _empty_page()
        page_limit = _bounded_limit(query.limit)
        session_ids = [stable]
        if query.include_ancestors:
            session_ids = self._lineage_root_to_tip(stable)
        if not session_ids:
            return _empty_page()

        normalized_direction = str(query.direction or "tail").strip().lower()
        if normalized_direction not in {"tail", "before", "after"}:
            normalized_direction = "tail"

        placeholders = ",".join("?" for _ in session_ids)
        params: tuple[Any, ...] = tuple(session_ids)
        active_clause = "" if query.include_inactive else " AND active = 1"
        total_count = self._conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({placeholders})"
            f"{active_clause}",
            params,
        ).fetchone()[0]

        columns = _conversation_message_columns()
        if normalized_direction == "before" and query.cursor_id is not None:
            rows = self._conn.execute(
                f"SELECT {columns} FROM messages "
                f"WHERE session_id IN ({placeholders}) AND id < ? "
                f"{active_clause} "
                "ORDER BY id DESC LIMIT ?",
                params + (int(query.cursor_id), page_limit + 1),
            ).fetchall()
            selected_rows = list(reversed(rows[:page_limit]))
        elif normalized_direction == "after" and query.cursor_id is not None:
            rows = self._conn.execute(
                f"SELECT {columns} FROM messages "
                f"WHERE session_id IN ({placeholders}) AND id > ? "
                f"{active_clause} "
                "ORDER BY id ASC LIMIT ?",
                params + (int(query.cursor_id), page_limit + 1),
            ).fetchall()
            selected_rows = list(rows[:page_limit])
        else:
            rows = self._conn.execute(
                f"SELECT {columns} FROM messages "
                f"WHERE session_id IN ({placeholders}) "
                f"{active_clause} "
                "ORDER BY id DESC LIMIT ?",
                params + (page_limit + 1,),
            ).fetchall()
            selected_rows = list(reversed(rows[:page_limit]))

        selected_rows = self._expand_to_turn_boundaries(
            selected_rows,
            session_ids=session_ids,
            columns=columns,
            include_inactive=query.include_inactive,
        )
        first_id = int(selected_rows[0]["id"]) if selected_rows else None
        last_id = int(selected_rows[-1]["id"]) if selected_rows else None
        has_more_before = self._has_messages_on_side(
            session_ids,
            row_id=first_id,
            side="before",
            include_inactive=query.include_inactive,
        )
        has_more_after = self._has_messages_on_side(
            session_ids,
            row_id=last_id,
            side="after",
            include_inactive=query.include_inactive,
        )

        messages: list[dict[str, Any]] = []
        for row in selected_rows:
            message = _row_as_conversation(row, include_storage_metadata=True)
            if query.include_ancestors and _is_duplicate_replayed_user_message(messages, message):
                continue
            messages.append(message)

        return {
            "messages": messages,
            "pageInfo": {
                "prev_cursor_id": first_id if has_more_before else None,
                "next_cursor_id": last_id if has_more_after else None,
                "hasMoreBefore": has_more_before,
                "hasMoreAfter": has_more_after,
                "totalCount": int(total_count or 0),
            },
        }

    def all_as_conversation(
        self,
        session_id: str,
        *,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return self._all_as_conversation_locked(
                session_id,
                include_ancestors=include_ancestors,
                include_storage_metadata=include_storage_metadata,
                include_inactive=include_inactive,
            )

    def _all_as_conversation_locked(
        self,
        session_id: str,
        *,
        include_ancestors: bool,
        include_storage_metadata: bool,
        include_inactive: bool,
    ) -> list[dict[str, Any]]:
        stable = str(session_id or "").strip()
        if not stable:
            return []
        session_ids = [stable]
        if include_ancestors:
            session_ids = self._lineage_root_to_tip(stable)
        if not session_ids:
            return []
        placeholders = ",".join("?" for _ in session_ids)
        active_clause = "" if include_inactive else " AND active = 1"
        rows = self._conn.execute(
            f"SELECT {_conversation_message_columns()} "
            f"FROM messages WHERE session_id IN ({placeholders})"
            f"{active_clause} ORDER BY id",
            tuple(session_ids),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            message = _row_as_conversation(
                row,
                include_storage_metadata=include_storage_metadata,
            )
            if include_ancestors and _is_duplicate_replayed_user_message(messages, message):
                continue
            messages.append(message)
        return messages

    def list_recent_user_messages(
        self,
        session_id: str,
        *,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        stable = str(session_id or "").strip()
        if not stable:
            return []
        bounded_limit = _bounded_limit(limit)
        active_clause = "" if include_inactive else " AND active = 1"
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, timestamp, content FROM messages "
                "WHERE session_id = ? AND role = 'user'"
                f"{active_clause} "
                "ORDER BY id DESC LIMIT ?",
                (stable, bounded_limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "preview": _message_preview(row["content"], 80),
            }
            for row in rows
        ]

    def _lineage_root_to_tip(self, session_id: str) -> list[str]:
        if not _table_exists(self._conn, "sessions"):
            return [session_id]
        if "parent_session_id" not in _table_columns(self._conn, "sessions"):
            return [session_id]
        ancestors: list[str] = []
        current = session_id
        seen: set[str] = set()
        for _ in range(64):
            if not current or current in seen:
                break
            seen.add(current)
            ancestors.append(current)
            row = self._conn.execute(
                "SELECT parent_session_id FROM sessions WHERE id = ?",
                (current,),
            ).fetchone()
            parent = _row_text(row, "parent_session_id", 0) if row is not None else ""
            if not parent:
                break
            current = parent
        return list(reversed(ancestors)) or [session_id]

    def _expand_to_turn_boundaries(
        self,
        rows: list[Any],
        *,
        session_ids: list[str],
        columns: str,
        include_inactive: bool,
    ) -> list[Any]:
        if not rows:
            return rows
        selected_identity_values: dict[str, set[str]] = {
            "turn_id": set(),
            "run_id": set(),
            "client_message_id": set(),
        }
        for row in rows:
            identity = _message_row_turn_metadata(row)
            for key, value in identity.items():
                selected_identity_values[key].add(value)
        if not any(selected_identity_values.values()):
            return rows

        placeholders = ",".join("?" for _ in session_ids)
        active_clause = "" if include_inactive else " AND active = 1"
        candidate_rows = self._conn.execute(
            f"SELECT {columns} FROM messages "
            f"WHERE session_id IN ({placeholders}) AND metadata_json IS NOT NULL "
            f"{active_clause} "
            "ORDER BY id",
            tuple(session_ids),
        ).fetchall()

        rows_by_id = {int(row["id"]): row for row in rows}
        matched_min_id_by_session: dict[str, int] = {}
        matched_user_sessions: set[str] = set()
        for row in candidate_rows:
            identity = _message_row_turn_metadata(row)
            if not any(
                value in selected_identity_values[key]
                for key, value in identity.items()
                if key in selected_identity_values
            ):
                continue
            row_id = int(row["id"])
            rows_by_id[row_id] = row
            row_session_id = str(row["session_id"])
            current_min = matched_min_id_by_session.get(row_session_id)
            if current_min is None or row_id < current_min:
                matched_min_id_by_session[row_session_id] = row_id
            if row["role"] == "user":
                matched_user_sessions.add(row_session_id)

        for row_session_id, first_matched_id in matched_min_id_by_session.items():
            if row_session_id in matched_user_sessions:
                continue
            previous_user = self._conn.execute(
                f"SELECT {columns} FROM messages "
                "WHERE session_id = ? AND role = 'user' AND id < ? "
                f"{active_clause} "
                "ORDER BY id DESC LIMIT 1",
                (row_session_id, first_matched_id),
            ).fetchone()
            if previous_user is not None:
                rows_by_id[int(previous_user["id"])] = previous_user
        return [rows_by_id[row_id] for row_id in sorted(rows_by_id)]

    def _has_messages_on_side(
        self,
        session_ids: list[str],
        *,
        row_id: int | None,
        side: str,
        include_inactive: bool,
    ) -> bool:
        if row_id is None:
            return False
        placeholders = ",".join("?" for _ in session_ids)
        operator = "<" if side == "before" else ">"
        active_clause = "" if include_inactive else " AND active = 1"
        row = self._conn.execute(
            f"SELECT 1 FROM messages "
            f"WHERE session_id IN ({placeholders}) AND id {operator} ? "
            f"{active_clause} "
            "LIMIT 1",
            tuple(session_ids) + (row_id,),
        ).fetchone()
        return row is not None


def _conversation_message_columns() -> str:
    return (
        "id, session_id, role, content, participant_id, tool_call_id, tool_calls, "
        "tool_name, timestamp, finish_reason, reasoning, reasoning_content, "
        "reasoning_details, codex_reasoning_items, codex_message_items, "
        "platform_message_id, conversation_message_id, metadata_json"
    )


def _row_as_conversation(row: Any, *, include_storage_metadata: bool) -> dict[str, Any]:
    content = _decode_content(row["content"])
    if row["role"] in {"user", "assistant"} and isinstance(content, str):
        content = sanitize_context(content).strip()
    message: dict[str, Any] = {"role": row["role"], "content": content}
    if include_storage_metadata:
        message["message_id"] = str(row["id"])
        message["timestamp"] = row["timestamp"]
    elif row["platform_message_id"]:
        message["message_id"] = row["platform_message_id"]
    for source_key, target_key in (
        ("conversation_message_id", "conversation_message_id"),
        ("participant_id", "participant_id"),
        ("tool_call_id", "tool_call_id"),
        ("tool_name", "tool_name"),
    ):
        value = str(row[source_key] or "").strip()
        if value:
            message[target_key] = value
    if row["tool_calls"]:
        message["tool_calls"] = _json_or(row["tool_calls"], [])
    if row["role"] == "assistant":
        for source_key in (
            "finish_reason",
            "reasoning",
        ):
            if row[source_key] is not None and row[source_key] != "":
                message[source_key] = row[source_key]
        if row["reasoning_content"] is not None:
            message["reasoning_content"] = row["reasoning_content"]
        if row["reasoning_details"]:
            message["reasoning_details"] = _json_or(row["reasoning_details"], None)
        if row["codex_reasoning_items"]:
            message["codex_reasoning_items"] = _json_or(row["codex_reasoning_items"], None)
        if row["codex_message_items"]:
            message["codex_message_items"] = _json_or(row["codex_message_items"], None)
    if row["metadata_json"]:
        message["metadata"] = _json_or(row["metadata_json"], None)
    return message


def _message_row_turn_metadata(row: Any) -> dict[str, str]:
    raw_metadata = row["metadata_json"] if row is not None else ""
    if not raw_metadata:
        return {}
    metadata = _json_or(raw_metadata, {})
    if not isinstance(metadata, dict):
        return {}
    identity: dict[str, str] = {}
    for key in ("turn_id", "run_id", "client_message_id"):
        value = metadata.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            identity[key] = text
    return identity


def _is_duplicate_replayed_user_message(messages: list[dict[str, Any]], message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return False
    for previous in reversed(messages):
        if previous.get("role") == "user" and previous.get("content") == content:
            return True
        if previous.get("role") == "assistant" and (
            previous.get("content") or previous.get("tool_calls")
        ):
            return False
    return False


def _decode_content(value: Any) -> Any:
    return _decode_stored_content(value)


def _message_preview(value: Any, limit: int) -> str:
    decoded = _decode_content(value)
    if isinstance(decoded, list):
        parts = [
            str(part.get("text") or "")
            for part in decoded
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        preview = " ".join(part for part in parts if part).strip() or "[multimodal content]"
    elif isinstance(decoded, str):
        preview = decoded
    else:
        preview = ""
    preview = " ".join(preview.split())
    if len(preview) > limit:
        return preview[: max(0, limit - 3)] + "..."
    return preview


def _json_or(value: Any, default: Any) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _bounded_limit(value: int | str | None) -> int:
    try:
        limit = int(value) if value is not None else 50
    except (TypeError, ValueError):
        limit = 50
    return max(1, min(limit, 500))


def _empty_page() -> dict[str, Any]:
    return {
        "messages": [],
        "pageInfo": {
            "prev_cursor_id": None,
            "next_cursor_id": None,
            "hasMoreBefore": False,
            "hasMoreAfter": False,
            "totalCount": 0,
        },
    }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
    except Exception:
        return set()


def _row_text(row: Any, key: str, index: int) -> str:
    if isinstance(row, sqlite3.Row):
        return str(row[key] or "")
    return str(row[index] or "")


__all__ = ["MessageHistoryReadModel", "MessagePageQuery"]
