"""Conversation message application service.

This service is the public owner for transcript reads and writes. Callers use
it instead of depending on the storage composition root or legacy state
facades.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from hermes_agent.domain.transcript_visibility import (
    PublicTranscriptVisibilityPolicy,
    is_public_transcript_message,
)
from hermes_agent.read_models.message_history import (
    MessageHistoryReadModel,
    MessagePageQuery,
)
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.read_models.visible_transcript import (
    TranscriptVisibilityPolicy,
    VisibleConversationTranscriptReadModel,
)
from hermes_agent.repositories.message_content_codec import decode_message_content
from hermes_agent.repositories.message_repo import MessageRepoImpl, MessageRepository
from hermes_agent.repositories.session_repo import SessionRepo, SessionSpec
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class MessageService:
    """Coordinates canonical transcript writes and visible-history reads."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        sessions: SessionRepo,
        *,
        unit_of_work: SqliteUnitOfWork | None = None,
        visibility_policies: Mapping[str, TranscriptVisibilityPolicy] | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._sessions = sessions
        self._writer = MessageRepository(conn, sessions)
        self._message_repo = MessageRepoImpl(conn)
        self._unit_of_work = unit_of_work or SqliteUnitOfWork(conn, self._lock)
        self._history = MessageHistoryReadModel(conn)
        self._public_history = VisibleConversationTranscriptReadModel(
            conn,
            PublicTranscriptVisibilityPolicy(),
        )
        self._visible_histories = {
            str(conversation_kind or "")
            .strip()
            .lower(): VisibleConversationTranscriptReadModel(
                conn,
                policy,
            )
            for conversation_kind, policy in (visibility_policies or {}).items()
            if str(conversation_kind or "").strip()
        }
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
                row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM messages"
                ).fetchone()
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

    def list(
        self, session_id: str, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        return [
            message
            for message in self.runtime_list(
                session_id,
                include_inactive=include_inactive,
            )
            if is_public_transcript_message(message)
        ]

    def runtime_list(
        self, session_id: str, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        """Return raw storage rows for runtime/domain maintenance only."""

        active_clause = "" if include_inactive else " AND active = 1"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ?"
                f"{active_clause} ORDER BY id",
                (str(session_id or ""),),
            ).fetchall()
        return [_message_row(row) for row in rows]

    def window_around(
        self,
        session_id: str,
        message_id: int,
        *,
        window: int = 5,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        return self._recall.get_messages_around(
            session_id,
            message_id,
            window=window,
            include_inactive=include_inactive,
        )

    def anchored_view(
        self,
        session_id: str,
        message_id: int,
        *,
        window: int = 5,
        bookend: int = 3,
        keep_roles: tuple[str, ...] | None = ("user", "assistant"),
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        return self._recall.get_anchored_view(
            session_id,
            message_id,
            window=window,
            bookend=bookend,
            keep_roles=keep_roles,
            include_inactive=include_inactive,
        )

    def owning_session_id(self, message_id: int) -> str | None:
        return self._recall.session_id_for_message(message_id)

    def recent_user_messages(
        self,
        session_id: str,
        *,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        return self._history_for(session_id).list_recent_user_messages(
            session_id,
            limit=limit,
            include_inactive=include_inactive,
        )

    def rewind(self, session_id: str, target_message_id: int) -> dict[str, Any]:
        rewind_session_id = str(session_id or "").strip()
        stable_message_id = int(target_message_id or 0)
        if not rewind_session_id or stable_message_id <= 0:
            raise ValueError("session_id and target_message_id are required")

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ? AND session_id = ?",
                (stable_message_id, rewind_session_id),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"message {stable_message_id} not found in session {rewind_session_id}"
            )
        target_message = _message_row(row)
        if target_message.get("role") != "user":
            raise ValueError(
                "rewind target must be a 'user' message "
                f"(got role={target_message.get('role')!r}, id={stable_message_id})"
            )

        def _rewind(_conn: sqlite3.Connection) -> list[int]:
            rewound_ids = self._message_repo.deactivate_from(
                rewind_session_id,
                stable_message_id,
            )
            self._sessions.increment_rewind_count(rewind_session_id)
            self._writer.rebuild_session_projection(rewind_session_id)
            return rewound_ids

        rewound_ids = self._unit_of_work.execute(_rewind)
        with self._lock:
            head = self._conn.execute(
                "SELECT MAX(id) AS id FROM messages "
                "WHERE session_id = ? AND active = 1",
                (rewind_session_id,),
            ).fetchone()
        return {
            "rewound_count": len(rewound_ids),
            "target_message": target_message,
            "new_head_id": int(head["id"]) if head and head["id"] is not None else None,
        }

    def all_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        return self._history_for(session_id).all_as_conversation(
            session_id,
            include_ancestors=include_ancestors,
            include_storage_metadata=include_storage_metadata,
            include_inactive=include_inactive,
        )

    def runtime_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Load canonical model context, including durable internal messages.

        Public callers should use :meth:`all_as_conversation`. Runtime
        hydration uses this explicit raw projection so background completion
        context remains available to the model without leaking into the UI.
        """

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
        return self._history_for(session_id).page_as_conversation(
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
        content: Any = None,
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
            "api_content": fields.get("api_content")
            if isinstance(fields.get("api_content"), str)
            else None,
            "platform_message_id": fields.get("platform_message_id"),
            "conversation_message_id": fields.get("conversation_message_id"),
            "metadata": fields.get("metadata"),
            "timestamp": fields.get("timestamp"),
        }
        return self._writer.append_conversation_message(stable, message)

    def set_current_user_api_content(
        self,
        session_id: str,
        *,
        content: Any,
        api_content: str,
        conversation_message_id: str = "",
    ) -> int:
        return self._writer.set_current_user_api_content(
            session_id,
            content=content,
            api_content=api_content,
            conversation_message_id=conversation_message_id,
        )

    def _history_for(
        self,
        session_id: str,
    ) -> MessageHistoryReadModel | VisibleConversationTranscriptReadModel:
        session = self._sessions.get(str(session_id or "").strip())
        conversation_kind = (
            str(session.conversation_kind if session else "").strip().lower()
        )
        return self._visible_histories.get(conversation_kind, self._public_history)

    def replace(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        self._writer.replace_conversation(stable, messages)

    def compact_active(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> None:
        """Persist one non-destructive in-place compaction boundary."""
        stable = str(session_id or "").strip()
        if not stable:
            raise ValueError("session_id is required")
        self._writer.compact_active_conversation(
            stable,
            messages,
            system_prompt=system_prompt,
        )

    def merge_metadata(
        self,
        session_id: str,
        metadata: dict[str, Any],
        *,
        message_id: str | int | None = None,
        role: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        client_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._writer.merge_metadata(
            session_id,
            metadata,
            message_id=message_id,
            role=role,
            run_id=run_id,
            turn_id=turn_id,
            client_message_id=client_message_id,
        )

    def rewrite_content(
        self,
        session_id: str,
        content: Any,
        *,
        message_id: str | int | None = None,
        conversation_message_id: str | None = None,
        role: str | None = None,
        persist_message_key: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        turn_message_index: str | int | None = None,
    ) -> dict[str, Any] | None:
        """Rewrite one existing canonical row through exact storage identity."""
        return self._writer.rewrite_content(
            session_id,
            content,
            message_id=message_id,
            conversation_message_id=conversation_message_id,
            role=role,
            persist_message_key=persist_message_key,
            run_id=run_id,
            turn_id=turn_id,
            turn_message_index=turn_message_index,
        )

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
    item["content"] = decode_message_content(
        item.get("content"), allow_legacy_json=True
    )
    for key in (
        "tool_calls",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
    ):
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
