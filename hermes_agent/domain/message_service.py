"""Conversation message application service.

This service is the public owner for transcript reads and writes. Callers use
it instead of depending on the storage composition root or legacy state
facades.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_agent.read_models.message_history import MessageHistoryReadModel, MessagePageQuery
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.repositories.message_content_codec import decode_message_content
from hermes_agent.repositories.message_repo import MessageRepository
from hermes_agent.repositories.session_repo import SessionRepo, SessionSpec
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


class MessageService:
    """Coordinates canonical transcript writes and visible-history reads."""

    def __init__(self, conn: sqlite3.Connection, sessions: SessionRepo) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._sessions = sessions
        self._writer = MessageRepository(conn, sessions)
        self._history = MessageHistoryReadModel(conn)
        self._recall = SessionRecallReadModel(conn)

    def count(self, session_id: str | None = None) -> int:
        stable = str(session_id or "").strip()
        with self._lock:
            if stable:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM messages WHERE session_id = ?",
                    (stable,),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
        return int(row["count"] if row else 0)

    def search(
        self,
        query: str,
        source_filter: list[str] | None = None,
        exclude_sources: list[str] | None = None,
        role_filter: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        sort: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        return self._recall.search_messages(
            query,
            source_filter=source_filter,
            exclude_sources=exclude_sources,
            role_filter=role_filter,
            limit=limit,
            offset=offset,
            sort=sort,
            include_inactive=include_inactive,
        )

    def list(self, session_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        active_clause = "" if include_inactive else " AND active = 1"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ?"
                f"{active_clause} ORDER BY id",
                (str(session_id or ""),),
            ).fetchall()
        return [_message_row(row) for row in rows]

    def all_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        return self._history.all_as_conversation(
            session_id,
            include_ancestors=include_ancestors,
            include_storage_metadata=include_storage_metadata,
            include_inactive=include_inactive,
        )

    def page_as_conversation(
        self,
        session_id: str,
        direction: str = "tail",
        cursor_id: int | None = None,
        limit: int = 50,
        include_ancestors: bool = False,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        return self._history.page_as_conversation(
            session_id,
            MessagePageQuery(
                direction=direction,
                cursor_id=cursor_id,
                limit=limit,
                include_ancestors=include_ancestors,
                include_inactive=include_inactive,
            ),
        )

    def append(
        self,
        session_id: str,
        role: str,
        content: Any,
        **fields: Any,
    ) -> int:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        if self._sessions.get(stable) is None:
            self._sessions.create(SessionSpec(session_id=stable, source="cli"))
        message = {
            "role": str(role or "unknown"),
            "content": content,
            "participant_id": str(fields.get("participant_id") or ""),
            "tool_call_id": fields.get("tool_call_id"),
            "tool_calls": fields.get("tool_calls"),
            "tool_name": fields.get("tool_name"),
            "token_count": fields.get("token_count"),
            "finish_reason": fields.get("finish_reason"),
            "reasoning": fields.get("reasoning"),
            "reasoning_content": fields.get("reasoning_content"),
            "reasoning_details": fields.get("reasoning_details"),
            "codex_reasoning_items": fields.get("codex_reasoning_items"),
            "codex_message_items": fields.get("codex_message_items"),
            "platform_message_id": fields.get("platform_message_id"),
            "metadata": fields.get("metadata"),
        }
        return self._writer.append_conversation_message(stable, message)

    def replace(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        self._writer.replace_conversation(stable, messages)

    def upsert_team_message(
        self,
        *,
        session_id: str,
        conversation_message_id: str,
        role: str,
        content: Any,
        participant_id: str = "",
        metadata: dict[str, Any] | None = None,
        status: str = "",
        reasoning: Any = "",
        tool_calls: Any = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        return self._writer.upsert_team_message_by_id(
            session_id=session_id,
            conversation_message_id=conversation_message_id,
            role=role,
            content=content,
            participant_id=participant_id,
            metadata=dict(metadata or {}),
            status=status,
            reasoning=reasoning,
            tool_calls=tool_calls,
            timestamp=timestamp,
        )

    def upsert_team_message_locked(
        self,
        *,
        session_id: str,
        conversation_message_id: str,
        role: str,
        content: Any,
        participant_id: str = "",
        metadata: dict[str, Any] | None = None,
        status: str = "",
        reasoning: Any = "",
        tool_calls: Any = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        return self._writer.upsert_team_message_by_id_locked(
            session_id=session_id,
            conversation_message_id=conversation_message_id,
            role=role,
            content=content,
            participant_id=participant_id,
            metadata=dict(metadata or {}),
            status=status,
            reasoning=reasoning,
            tool_calls=tool_calls,
            timestamp=timestamp,
        )


def _message_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["content"] = decode_message_content(item.get("content"), allow_legacy_json=True)
    for key in ("tool_calls", "reasoning_details", "codex_reasoning_items", "codex_message_items"):
        if item.get(key):
            item[key] = _json_or(item[key], [])
    if item.get("metadata_json"):
        item["metadata"] = _json_or(item["metadata_json"], None)
    return item


def _json_or(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


__all__ = ["MessageService"]
