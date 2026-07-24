"""Policy-driven read model for canonical visible conversation transcripts."""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from hermes_agent.read_models.message_history import (
    MessageHistoryReadModel,
    MessagePageQuery,
)
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


class TranscriptVisibilityPolicy(Protocol):
    """Domain policy applied before transcript pagination."""

    def includes(self, message: dict[str, Any]) -> bool: ...

    def stable_message_id(self, message: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class VisibleTranscriptQuery:
    direction: str = "tail"
    cursor_id: int | None = None
    limit: int = 50
    include_ancestors: bool = False
    include_inactive: bool = False


class VisibleConversationTranscriptReadModel:
    """Projects, orders, deduplicates, and pages one visible transcript.

    Filtering happens before pagination so internal activity rows cannot evict
    user-visible turns or corrupt page counts. Event time is the canonical
    display order; the SQLite row id is the deterministic tie breaker and
    opaque page cursor.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        policy: TranscriptVisibilityPolicy,
    ) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._history = MessageHistoryReadModel(conn)
        self._policy = policy

    def all_as_conversation(
        self,
        session_id: str,
        *,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            messages = self._visible_messages(
                session_id,
                include_ancestors=include_ancestors,
                include_inactive=include_inactive,
            )
        if include_storage_metadata:
            return messages
        return [_without_storage_metadata(message) for message in messages]

    def page_as_conversation(
        self,
        session_id: str,
        query: VisibleTranscriptQuery | MessagePageQuery,
    ) -> dict[str, Any]:
        direction = _normalized_direction(query.direction)
        limit = _bounded_limit(query.limit)
        with self._lock:
            messages = self._visible_messages(
                session_id,
                include_ancestors=query.include_ancestors,
                include_inactive=query.include_inactive,
            )
            selected, has_more_before, has_more_after = self._select_page(
                messages,
                direction=direction,
                cursor_id=query.cursor_id,
                limit=limit,
            )

        first_id = _storage_id(selected[0]) if selected else None
        last_id = _storage_id(selected[-1]) if selected else None
        return {
            "messages": selected,
            "pageInfo": {
                "prev_cursor_id": first_id if has_more_before else None,
                "next_cursor_id": last_id if has_more_after else None,
                "hasMoreBefore": has_more_before,
                "hasMoreAfter": has_more_after,
                "totalCount": len(messages),
            },
        }

    def list_recent_user_messages(
        self,
        session_id: str,
        *,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Return recent public user turns for retry/rewind commands."""

        bounded_limit = _bounded_limit(limit)
        with self._lock:
            messages = self._visible_messages(
                session_id,
                include_ancestors=False,
                include_inactive=include_inactive,
            )
        recent = [
            message
            for message in reversed(messages)
            if str(message.get("role") or "").strip().lower() == "user"
        ][:bounded_limit]
        return [
            {
                "id": _storage_id(message),
                "timestamp": message.get("timestamp"),
                "preview": _message_preview(message.get("content"), 80),
            }
            for message in recent
        ]

    def _visible_messages(
        self,
        session_id: str,
        *,
        include_ancestors: bool,
        include_inactive: bool,
    ) -> list[dict[str, Any]]:
        messages = self._history.all_as_conversation(
            session_id,
            include_ancestors=include_ancestors,
            include_storage_metadata=True,
            include_inactive=include_inactive,
        )
        messages.sort(key=_message_order_key)

        visible: list[dict[str, Any]] = []
        stable_positions: dict[str, int] = {}
        for raw_message in messages:
            message = dict(raw_message)
            if not self._policy.includes(message):
                continue
            stable_id = self._policy.stable_message_id(message)
            if not stable_id:
                visible.append(message)
                continue
            existing_position = stable_positions.get(stable_id)
            if existing_position is None:
                stable_positions[stable_id] = len(visible)
                visible.append(message)
                continue
            existing = visible[existing_position]
            if not _has_authoritative_stable_id(
                existing
            ) and _has_authoritative_stable_id(message):
                visible[existing_position] = message
        return visible

    def _select_page(
        self,
        messages: list[dict[str, Any]],
        *,
        direction: str,
        cursor_id: int | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        if direction == "tail" or cursor_id is None:
            start = max(0, len(messages) - limit)
            return messages[start:], start > 0, False

        cursor_position, exact = self._cursor_position(messages, cursor_id)
        if direction == "before":
            end = cursor_position
            start = max(0, end - limit)
            return messages[start:end], start > 0, end < len(messages)

        start = cursor_position + 1 if exact else cursor_position
        end = min(len(messages), start + limit)
        return messages[start:end], start > 0, end < len(messages)

    def _cursor_position(
        self,
        messages: list[dict[str, Any]],
        cursor_id: int,
    ) -> tuple[int, bool]:
        numeric_cursor = int(cursor_id)
        for index, message in enumerate(messages):
            if _storage_id(message) == numeric_cursor:
                return index, True

        row = self._conn.execute(
            "SELECT timestamp, id FROM messages WHERE id = ?",
            (numeric_cursor,),
        ).fetchone()
        if row is None:
            raise ValueError(f"message cursor {numeric_cursor} does not exist")

        cursor_key = (float(row["timestamp"] or 0), int(row["id"]))
        keys = [_message_order_key(message) for message in messages]
        return bisect.bisect_left(keys, cursor_key), False


def _message_order_key(message: dict[str, Any]) -> tuple[float, int]:
    try:
        timestamp = float(message.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0.0
    return timestamp, _storage_id(message) or 0


def _storage_id(message: dict[str, Any]) -> int | None:
    value = message.get("message_id") or message.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_authoritative_stable_id(message: dict[str, Any]) -> bool:
    return bool(
        str(
            message.get("conversation_message_id")
            or message.get("conversationMessageId")
            or ""
        ).strip()
    )


def _message_preview(value: Any, limit: int) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = " ".join(
            str(part.get("text") or "")
            for part in value
            if isinstance(part, dict)
            and part.get("type") in {"text", "input_text", "output_text"}
        )
    else:
        text = str(value or "")
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(0, limit - 1)]}…"


def _without_storage_metadata(message: dict[str, Any]) -> dict[str, Any]:
    projected = dict(message)
    projected.pop("message_id", None)
    projected.pop("timestamp", None)
    return projected


def _normalized_direction(value: Any) -> str:
    direction = str(value or "tail").strip().lower()
    return direction if direction in {"tail", "before", "after"} else "tail"


def _bounded_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 50
    return max(1, min(limit, 500))


__all__ = [
    "TranscriptVisibilityPolicy",
    "VisibleConversationTranscriptReadModel",
    "VisibleTranscriptQuery",
]
