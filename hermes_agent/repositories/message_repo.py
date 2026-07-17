"""SQLite message write repository.

This owner replaces gateway calls into the legacy DB facade for targeted
message mutations. Read projection remains in ``MessageHistoryReadModel``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from agent.memory_manager import sanitize_context
from hermes_agent.repositories.base import RepositoryConnection
from hermes_agent.repositories.message_content_codec import decode_message_content
from hermes_agent.repositories.message_content_codec import encode_message_content
from hermes_agent.repositories.session_repo import (
    SessionMessageAppendProjection,
    SessionMessageSnapshotProjection,
    SessionRepo,
    SessionRepoImpl,
)
from hermes_agent.domain.tool_effect import persist_tool_effect, tool_effect_from_metadata
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection


class PageDirection(str, Enum):
    HEAD = "head"
    TAIL = "tail"


@dataclass(frozen=True)
class MessageSpec:
    session_id: str
    role: str
    content: str = ""
    participant_id: str = ""
    tool_call_id: str = ""
    tool_calls: str = ""
    tool_name: str = ""
    effect_disposition: str = ""
    reasoning: str = ""
    conversation_message_id: str = ""
    platform_message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass(frozen=True)
class Message:
    id: int
    session_id: str
    role: str
    content: str
    participant_id: str
    timestamp: float
    tool_call_id: str = ""
    tool_calls: str = ""
    tool_name: str = ""
    effect_disposition: str = ""
    reasoning: str = ""
    conversation_message_id: str = ""
    platform_message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    active: bool = True


@dataclass(frozen=True)
class MessagePage:
    messages: tuple[Message, ...]
    next_cursor_id: int | None
    prev_cursor_id: int | None
    has_more: bool


@runtime_checkable
class MessageRepo(Protocol):
    def append(self, session_id: str, message: MessageSpec) -> Message: ...

    def get_page(
        self,
        session_id: str,
        *,
        cursor_id: int | None = None,
        direction: PageDirection = PageDirection.TAIL,
        limit: int = 50,
        include_ancestors: bool = True,
    ) -> MessagePage: ...

    def search_fts(self, query: str, **filters: Any) -> list[Message]: ...

    def replace_all(self, session_id: str, history: list[MessageSpec]) -> None: ...

    def merge_metadata(
        self,
        session_id: str,
        message_id: int,
        patch: dict[str, Any],
    ) -> Message: ...

    def copy_branch_prefix(
        self,
        source_session_ids: list[str],
        included_row_id: int,
        target_session_id: str,
        copy_started_at: float,
    ) -> int: ...

    def delete_by_session(self, session_id: str) -> int: ...

    def deactivate_from(self, session_id: str, since_message_id: int) -> list[int]: ...

    def restore_from(self, session_id: str, since_message_id: int) -> int: ...


class MessageRepoImpl:
    """SQLite-backed MessageRepo (spec §4.3)."""

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def append(self, session_id: str, message: MessageSpec) -> Message:
        stable_sid = str(session_id or "").strip()
        role = str(message.role or "").strip()
        if not stable_sid or not role:
            raise ValueError("session_id and role are required")
        ts = float(message.timestamp or time.time())
        metadata_json = json.dumps(
            persist_tool_effect(message.metadata, message.effect_disposition),
            ensure_ascii=False,
        )
        cursor = self._conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, participant_id, tool_call_id,
                tool_calls, tool_name, timestamp, reasoning,
                conversation_message_id, platform_message_id, metadata_json,
                active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                stable_sid,
                role,
                str(message.content or ""),
                str(message.participant_id or ""),
                str(message.tool_call_id or ""),
                str(message.tool_calls or ""),
                str(message.tool_name or ""),
                ts,
                str(message.reasoning or ""),
                str(message.conversation_message_id or ""),
                str(message.platform_message_id or ""),
                metadata_json,
            ),
        )
        last_row = cursor.lastrowid
        if last_row is None:
            raise RuntimeError("INSERT INTO messages returned no lastrowid")
        got = self._fetch_by_id(int(last_row))
        assert got is not None
        return got

    def get_page(
        self,
        session_id: str,
        *,
        cursor_id: int | None = None,
        direction: PageDirection = PageDirection.TAIL,
        limit: int = 50,
        include_ancestors: bool = True,
    ) -> MessagePage:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return MessagePage(messages=(), next_cursor_id=None, prev_cursor_id=None, has_more=False)
        limit = max(1, min(int(limit), 500))
        clauses = ["session_id = ?", "active = 1"]
        params: list[Any] = [stable_sid]
        if cursor_id is not None:
            clauses.append("id < ?" if direction is PageDirection.TAIL else "id > ?")
            params.append(int(cursor_id))
        order = "ASC" if direction is PageDirection.HEAD else "DESC"
        sql = (
            "SELECT id, session_id, role, content, participant_id, tool_call_id, "
            "tool_calls, tool_name, timestamp, reasoning, conversation_message_id, "
            "platform_message_id, metadata_json, active "
            "FROM messages "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY id {order} "
            "LIMIT ?"
        )
        params.append(limit + 1)
        rows = self._conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        messages = [_row_to_message(row) for row in rows]
        if direction is PageDirection.TAIL:
            messages.reverse()
        next_cursor_id: int | None = None
        prev_cursor_id: int | None = None
        if messages:
            first_id = messages[0].id
            last_id = messages[-1].id
            next_cursor_id = first_id if direction is PageDirection.TAIL else last_id
            prev_cursor_id = last_id if direction is PageDirection.TAIL else first_id
        return MessagePage(
            messages=tuple(messages),
            next_cursor_id=next_cursor_id if has_more else None,
            prev_cursor_id=prev_cursor_id,
            has_more=has_more,
        )

    def search_fts(self, query: str, **filters: Any) -> list[Message]:
        q = str(query or "").strip()
        if not q:
            return []
        session_id = filters.get("session_id")
        clauses = ["active = 1", "content LIKE ?"]
        params: list[Any] = [f"%{q}%"]
        if session_id:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        limit = int(filters.get("limit") or 50)
        sql = (
            "SELECT id, session_id, role, content, participant_id, tool_call_id, "
            "tool_calls, tool_name, timestamp, reasoning, conversation_message_id, "
            "platform_message_id, metadata_json, active "
            "FROM messages "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC "
            "LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_message(row) for row in rows]

    def replace_all(self, session_id: str, history: list[MessageSpec]) -> None:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            raise ValueError("session_id is required")
        self._conn.execute("UPDATE messages SET active = 0 WHERE session_id = ?", (stable_sid,))
        for spec in history:
            self.append(stable_sid, spec)

    def merge_metadata(
        self,
        session_id: str,
        message_id: int,
        patch: dict[str, Any],
    ) -> Message:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            raise ValueError("session_id is required")
        row = self._conn.execute(
            "SELECT metadata_json FROM messages WHERE id = ? AND session_id = ?",
            (int(message_id), stable_sid),
        ).fetchone()
        if row is None:
            raise LookupError(f"message {message_id} not found in session {stable_sid!r}")
        current_raw = row["metadata_json"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            current = json.loads(current_raw) if current_raw else {}
        except json.JSONDecodeError:
            current = {}
        if not isinstance(current, dict):
            current = {}
        merged = {**current, **(patch or {})}
        self._conn.execute(
            "UPDATE messages SET metadata_json = ? WHERE id = ? AND session_id = ?",
            (json.dumps(merged, ensure_ascii=False), int(message_id), stable_sid),
        )
        got = self._fetch_by_id(int(message_id))
        assert got is not None
        return got

    def copy_branch_prefix(
        self,
        source_session_ids: list[str],
        included_row_id: int,
        target_session_id: str,
        copy_started_at: float,
    ) -> int:
        stable_sources = [str(sid or "").strip() for sid in source_session_ids if str(sid or "").strip()]
        stable_target = str(target_session_id or "").strip()
        if not stable_sources:
            raise ValueError("source_session_ids are required")
        if not stable_target:
            raise ValueError("target_session_id is required")
        row_limit = int(included_row_id or 0)
        if row_limit <= 0:
            raise ValueError("included_row_id must be positive")
        placeholders = ",".join("?" for _ in stable_sources)
        params: tuple[Any, ...] = tuple(stable_sources) + (row_limit,)
        count_row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS n
              FROM messages
             WHERE session_id IN ({placeholders})
               AND id <= ?
            """,
            params,
        ).fetchone()
        copied_count = int(count_row["n"] if isinstance(count_row, sqlite3.Row) else count_row[0])
        if copied_count <= 0:
            return 0
        self._conn.execute(
            f"""
            INSERT INTO messages (
                session_id, role, content, tool_call_id, tool_calls, tool_name,
                timestamp, token_count, finish_reason, reasoning, reasoning_content,
                reasoning_details, codex_reasoning_items, codex_message_items,
                platform_message_id, metadata_json
            )
            SELECT
                ?, role, content, tool_call_id, tool_calls, tool_name,
                ? + (ROW_NUMBER() OVER (ORDER BY id) * 0.000001),
                token_count, finish_reason, reasoning,
                reasoning_content, reasoning_details, codex_reasoning_items,
                codex_message_items, platform_message_id, metadata_json
            FROM messages
            WHERE session_id IN ({placeholders}) AND id <= ?
            ORDER BY id
            """,
            (stable_target, float(copy_started_at or time.time())) + params,
        )
        return copied_count

    def delete_by_session(self, session_id: str) -> int:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return 0
        try:
            cursor = self._conn.execute("DELETE FROM messages WHERE session_id = ?", (stable_sid,))
        except sqlite3.OperationalError as exc:
            if "no such table: messages" in str(exc).lower():
                return 0
            raise
        return int(cursor.rowcount or 0)

    def deactivate_from(self, session_id: str, since_message_id: int) -> list[int]:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return []
        rows = self._conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND id >= ? AND active = 1",
            (stable_sid, int(since_message_id)),
        ).fetchall()
        ids = [int(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        self._conn.execute(
            f"UPDATE messages SET active = 0 WHERE id IN ({placeholders})",
            ids,
        )
        return ids

    def restore_from(self, session_id: str, since_message_id: int) -> int:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return 0
        rows = self._conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND id >= ? AND active = 0",
            (stable_sid, int(since_message_id)),
        ).fetchall()
        ids = [int(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = self._conn.execute(
            f"UPDATE messages SET active = 1 WHERE id IN ({placeholders})",
            ids,
        )
        return int(cursor.rowcount or 0)

    def _fetch_by_id(self, message_id: int) -> Message | None:
        row = self._conn.execute(
            """
            SELECT id, session_id, role, content, participant_id, tool_call_id,
                   tool_calls, tool_name, timestamp, reasoning,
                   conversation_message_id, platform_message_id, metadata_json,
                   active
              FROM messages
             WHERE id = ?
            """,
            (int(message_id),),
        ).fetchone()
        if row is None:
            return None
        return _row_to_message(row)


class MessageRepository:
    def __init__(self, conn: sqlite3.Connection, session_repo: SessionRepo | None = None) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._sessions = session_repo if session_repo is not None else SessionRepoImpl(conn)

    def append_conversation_message(self, session_id: str, message: dict[str, Any]) -> int:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            raise ValueError("session_id is required")
        role = str(message.get("role") or "unknown")
        timestamp = _message_timestamp(message, time.time())
        participant_id = str(message.get("participant_id") or "").strip()
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        with self._lock:
            self._begin_write()
            try:
                existing_id = self._select_existing_message_id_for_persist_key(
                    stable_sid,
                    role=role,
                    metadata=metadata,
                )
                if existing_id is not None:
                    self._conn.commit()
                    return existing_id

                projected_id = self._select_projected_team_message_id_for_append(
                    stable_sid,
                    role=role,
                    message=message,
                    participant_id=participant_id,
                    metadata=metadata,
                )
                if projected_id is not None:
                    self._merge_projected_message(
                        projected_id,
                        participant_id=participant_id,
                        metadata=metadata,
                        reasoning=str(message.get("reasoning") or ""),
                    )
                    self._conn.commit()
                    return projected_id

                message_id = self._insert_message(stable_sid, message, timestamp)
                self._sessions.record_message_append(
                    stable_sid,
                    SessionMessageAppendProjection(
                        timestamp=timestamp,
                        tool_call_count=_tool_call_count(message.get("tool_calls")),
                        user_preview=_message_preview_text(message.get("content"))
                        if role == "user"
                        else "",
                        user_display_title=_message_display_title_text(message.get("content"))
                        if role == "user"
                        else "",
                    ),
                )
                self._conn.commit()
                return message_id
            except Exception:
                self._conn.rollback()
                raise

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
        if not metadata:
            return None
        with self._lock:
            row = self._select_metadata_target(
                session_id,
                message_id=message_id,
                role=role,
                run_id=run_id,
                turn_id=turn_id,
                client_message_id=client_message_id,
            )
            if row is None:
                return None
            next_metadata = _merge_metadata(_row_metadata(row), metadata)
            raw = json.dumps(next_metadata, ensure_ascii=False)
            self._conn.execute(
                "UPDATE messages SET metadata_json = ? WHERE id = ?",
                (raw, row["id"]),
            )
            self._conn.commit()
            updated = dict(row)
            updated["metadata_json"] = raw
            return _row_as_conversation(updated, include_storage_metadata=True)

    def replace_conversation(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self._begin_write()
            try:
                self._replace_conversation_locked(session_id, messages)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def rebuild_session_projection(self, session_id: str) -> None:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return
        rows = self._conn.execute(
            """
            SELECT role, content, tool_calls, timestamp
              FROM messages
             WHERE session_id = ?
               AND active = 1
             ORDER BY timestamp ASC, id ASC
            """,
            (stable_sid,),
        ).fetchall()
        message_count = len(rows)
        tool_call_count = 0
        first_user_preview = ""
        first_user_display_title = ""
        last_message_ts: float | None = None
        for row in rows:
            role = str(row["role"] if isinstance(row, sqlite3.Row) else row[0] or "")
            content = row["content"] if isinstance(row, sqlite3.Row) else row[1]
            tool_calls = row["tool_calls"] if isinstance(row, sqlite3.Row) else row[2]
            timestamp = row["timestamp"] if isinstance(row, sqlite3.Row) else row[3]
            if role == "user" and not first_user_preview and content is not None:
                decoded = _decode_content(content)
                first_user_preview = _message_preview_text(decoded)
                first_user_display_title = _message_display_title_text(decoded)
            if tool_calls:
                tool_call_count += _tool_call_count(tool_calls)
            if timestamp is not None:
                last_message_ts = float(timestamp)
        self._sessions.replace_message_projection(
            stable_sid,
            SessionMessageSnapshotProjection(
                message_count=message_count,
                tool_call_count=tool_call_count,
                first_user_preview=first_user_preview,
                first_user_display_title=first_user_display_title,
                last_message_ts=last_message_ts,
            ),
        )

    def upsert_team_message_by_id(
        self,
        *,
        session_id: str,
        conversation_message_id: str,
        role: str,
        content: Any,
        participant_id: str,
        metadata: dict[str, Any],
        status: str = "",
        reasoning: Any = "",
        tool_calls: Any = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        stable_sid = str(session_id or "").strip()
        stable_message_id = str(conversation_message_id or "").strip()
        if not stable_sid:
            raise ValueError("session_id is required")
        if not stable_message_id:
            raise ValueError("conversation_message_id is required")
        with self._lock:
            self._begin_write()
            try:
                row = self._upsert_team_message_by_id_locked(
                    session_id=stable_sid,
                    conversation_message_id=stable_message_id,
                    role=role,
                    content=content,
                    participant_id=participant_id,
                    metadata=metadata,
                    status=status,
                    reasoning=reasoning,
                    tool_calls=tool_calls,
                )
                self._conn.commit()
                return row
            except Exception:
                self._conn.rollback()
                raise

    def upsert_team_message_by_id_locked(
        self,
        *,
        session_id: str,
        conversation_message_id: str,
        role: str,
        content: Any,
        participant_id: str,
        metadata: dict[str, Any],
        status: str = "",
        reasoning: Any = "",
        tool_calls: Any = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        stable_sid = str(session_id or "").strip()
        stable_message_id = str(conversation_message_id or "").strip()
        if not stable_sid:
            raise ValueError("session_id is required")
        if not stable_message_id:
            raise ValueError("conversation_message_id is required")
        return self._upsert_team_message_by_id_locked(
            session_id=stable_sid,
            conversation_message_id=stable_message_id,
            role=role,
            content=content,
            participant_id=participant_id,
            metadata=metadata,
            status=status,
            reasoning=reasoning,
            tool_calls=tool_calls,
            timestamp=timestamp,
        )

    def _replace_conversation_locked(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        total_messages = 0
        total_tool_calls = 0
        first_user_preview = ""
        first_user_display_title = ""
        last_message_ts: float | None = None
        fallback_ts = time.time()
        for index, message in enumerate(messages):
            timestamp = _message_timestamp(message, fallback_ts + index * 1e-6)
            self._insert_message(session_id, message, timestamp)
            total_messages += 1
            role = str(message.get("role") or "unknown")
            if role == "user" and not first_user_preview:
                first_user_preview = _message_preview_text(message.get("content"))
                first_user_display_title = _message_display_title_text(message.get("content"))
            tool_calls = message.get("tool_calls")
            if tool_calls is not None:
                total_tool_calls += len(tool_calls) if isinstance(tool_calls, list) else 1
            last_message_ts = timestamp
        self._sessions.replace_message_projection(
            session_id,
            SessionMessageSnapshotProjection(
                message_count=total_messages,
                tool_call_count=total_tool_calls,
                first_user_preview=first_user_preview,
                first_user_display_title=first_user_display_title,
                last_message_ts=last_message_ts,
            ),
        )

    def _upsert_team_message_by_id_locked(
        self,
        *,
        session_id: str,
        conversation_message_id: str,
        role: str,
        content: Any,
        participant_id: str,
        metadata: dict[str, Any],
        status: str,
        reasoning: Any,
        tool_calls: Any,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        normalized_role = str(role or "assistant").strip() or "assistant"
        normalized_participant_id = str(participant_id or "").strip()
        next_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        next_metadata["session_id"] = session_id
        next_metadata["conversation_message_id"] = conversation_message_id
        projection_status = str(status or "").strip()
        if projection_status:
            next_metadata["projection_status"] = projection_status
        metadata_json = json.dumps(next_metadata, ensure_ascii=False) if next_metadata else None
        stored_content = _encode_content(content)
        stored_reasoning = str(reasoning or "")
        message_timestamp = float(timestamp) if timestamp is not None else time.time()
        stored_tool_calls = _json_or_none(tool_calls) if tool_calls is not None else None

        existing = self._select_team_message_by_conversation_id(
            session_id,
            conversation_message_id,
        )
        if existing is None:
            existing = self._select_legacy_team_shadow_row(
                session_id=session_id,
                role=normalized_role,
                content=content,
                participant_id=normalized_participant_id,
                metadata=next_metadata,
            )
        if existing is None:
            cursor = self._conn.execute(
                """
                INSERT INTO messages (
                    session_id, role, content, participant_id, timestamp,
                    conversation_message_id, metadata_json, reasoning, tool_calls
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    normalized_role,
                    stored_content,
                    normalized_participant_id,
                    message_timestamp,
                    conversation_message_id,
                    metadata_json,
                    stored_reasoning,
                    stored_tool_calls,
                ),
            )
            row_id = int(cursor.lastrowid or 0)
            self._sessions.record_message_append(
                session_id,
                SessionMessageAppendProjection(timestamp=message_timestamp),
            )
        else:
            merged_metadata = _merge_metadata(
                _json_or(existing["metadata_json"], {}),
                next_metadata,
            )
            next_reasoning = stored_reasoning or str(existing["reasoning"] or "")
            next_tool_calls = (
                stored_tool_calls
                if stored_tool_calls is not None
                else existing["tool_calls"]
            )
            self._conn.execute(
                """
                UPDATE messages
                   SET role = ?,
                       content = ?,
                       participant_id = ?,
                       timestamp = ?,
                       conversation_message_id = ?,
                       metadata_json = ?,
                       reasoning = ?,
                       tool_calls = ?,
                       active = 1
                 WHERE id = ?
                """,
                (
                    normalized_role,
                    stored_content,
                    normalized_participant_id,
                    message_timestamp,
                    conversation_message_id,
                    json.dumps(merged_metadata, ensure_ascii=False),
                    next_reasoning,
                    next_tool_calls,
                    int(existing["id"]),
                ),
            )
            row_id = int(existing["id"])
            self._sessions.touch_message_activity(session_id, message_timestamp)
        row = self._conn.execute(
            f"SELECT {_conversation_message_columns()} FROM messages WHERE id = ?",
            (row_id,),
        ).fetchone()
        return _row_as_conversation(row, include_storage_metadata=True)

    def _select_metadata_target(
        self,
        session_id: str,
        *,
        message_id: str | int | None,
        role: str | None,
        run_id: str | None,
        turn_id: str | None,
        client_message_id: str | None,
    ) -> sqlite3.Row | None:
        target_message_id = str(message_id or "").strip()
        if target_message_id:
            try:
                numeric_message_id = int(target_message_id)
            except (TypeError, ValueError):
                numeric_message_id = None
            if numeric_message_id is not None:
                row = self._conn.execute(
                    "SELECT * FROM messages WHERE id = ? AND session_id = ?",
                    (numeric_message_id, session_id),
                ).fetchone()
                if row is not None:
                    return row
        target_run_id = str(run_id or "").strip()
        target_turn_id = str(turn_id or "").strip()
        target_client_message_id = str(client_message_id or "").strip()
        if not (target_run_id or target_turn_id or target_client_message_id):
            return None
        target_role = str(role or "").strip()
        active_clause = "AND role = ?" if target_role else ""
        params: list[Any] = [session_id]
        if target_role:
            params.append(target_role)
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? "
            f"{active_clause} "
            "AND metadata_json IS NOT NULL ORDER BY id DESC",
            tuple(params),
        ).fetchall()
        for row in rows:
            if _metadata_matches(
                _row_metadata(row),
                run_id=target_run_id,
                turn_id=target_turn_id,
                client_message_id=target_client_message_id,
            ):
                return row
        return None

    def _select_team_message_by_conversation_id(
        self,
        session_id: str,
        conversation_message_id: str,
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            f"SELECT {_conversation_message_columns()} "
            "FROM messages "
            "WHERE session_id = ? AND conversation_message_id = ? "
            "ORDER BY id LIMIT 1",
            (session_id, conversation_message_id),
        ).fetchone()

    def _select_legacy_team_shadow_row(
        self,
        *,
        session_id: str,
        role: str,
        content: Any,
        participant_id: str,
        metadata: dict[str, Any],
    ) -> sqlite3.Row | None:
        run_id = _metadata_text(metadata, "run_id", "runId")
        if not run_id:
            return None
        turn_id = _metadata_text(metadata, "turn_id", "turnId")
        rows = self._conn.execute(
            f"SELECT {_conversation_message_columns()} "
            "FROM messages "
            "WHERE session_id = ? "
            "  AND role = ? "
            "  AND COALESCE(conversation_message_id, '') = '' "
            "  AND active = 1 "
            "ORDER BY id",
            (session_id, role),
        ).fetchall()
        for row in rows:
            existing_metadata = _json_or(row["metadata_json"], {})
            if not isinstance(existing_metadata, dict):
                continue
            if _metadata_text(existing_metadata, "run_id", "runId") != run_id:
                continue
            existing_turn_id = _metadata_text(existing_metadata, "turn_id", "turnId")
            if turn_id and existing_turn_id and existing_turn_id != turn_id:
                continue
            existing_participant_id = str(row["participant_id"] or "").strip()
            if participant_id and existing_participant_id and existing_participant_id != participant_id:
                continue
            if _decode_content(row["content"]) != content:
                continue
            return row
        return None

    def _insert_message(self, session_id: str, message: dict[str, Any], timestamp: float) -> int:
        role = str(message.get("role") or "unknown")
        tool_calls = message.get("tool_calls")
        cursor = self._conn.execute(
            """INSERT INTO messages (
                session_id, role, content, participant_id, tool_call_id,
                tool_calls, tool_name, timestamp, token_count, finish_reason,
                reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                codex_message_items, platform_message_id, conversation_message_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                _encode_content(message.get("content")),
                str(message.get("participant_id") or ""),
                message.get("tool_call_id"),
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                message.get("tool_name"),
                timestamp,
                message.get("token_count"),
                message.get("finish_reason"),
                message.get("reasoning") if role == "assistant" else None,
                message.get("reasoning_content") if role == "assistant" else None,
                _json_or_none(message.get("reasoning_details")) if role == "assistant" else None,
                _json_or_none(message.get("codex_reasoning_items")) if role == "assistant" else None,
                _json_or_none(message.get("codex_message_items")) if role == "assistant" else None,
                _platform_message_id(message),
                str(message.get("conversation_message_id") or ""),
                _json_or_none(
                    persist_tool_effect(
                        message.get("metadata")
                        if isinstance(message.get("metadata"), dict)
                        else {},
                        message.get("effect_disposition"),
                    )
                ),
            ),
        )
        return int(cursor.lastrowid or 0)

    def _begin_write(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _select_existing_message_id_for_persist_key(
        self,
        session_id: str,
        *,
        role: str,
        metadata: dict[str, Any],
    ) -> int | None:
        persist_key = _metadata_text(metadata, "persist_message_key", "persistMessageKey")
        run_id = _metadata_text(metadata, "run_id", "runId")
        turn_id = _metadata_text(metadata, "turn_id", "turnId")
        turn_message_index = _metadata_text(
            metadata,
            "turn_message_index",
            "turnMessageIndex",
        )
        if persist_key:
            row = self._conn.execute(
                f"SELECT {_conversation_message_columns()} "
                "FROM messages "
                "WHERE session_id = ? "
                "  AND role = ? "
                "  AND active = 1 "
                "  AND COALESCE(json_extract(metadata_json, '$.persist_message_key'), json_extract(metadata_json, '$.persistMessageKey')) = ? "
                "ORDER BY id DESC "
                "LIMIT 1",
                (session_id, role, persist_key),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        if run_id and turn_id and turn_message_index:
            row = self._conn.execute(
                f"SELECT {_conversation_message_columns()} "
                "FROM messages "
                "WHERE session_id = ? "
                "  AND role = ? "
                "  AND active = 1 "
                "  AND COALESCE(json_extract(metadata_json, '$.run_id'), json_extract(metadata_json, '$.runId')) = ? "
                "  AND COALESCE(json_extract(metadata_json, '$.turn_id'), json_extract(metadata_json, '$.turnId')) = ? "
                "  AND CAST(COALESCE(json_extract(metadata_json, '$.turn_message_index'), json_extract(metadata_json, '$.turnMessageIndex')) AS TEXT) = ? "
                "ORDER BY id DESC "
                "LIMIT 1",
                (session_id, role, run_id, turn_id, turn_message_index),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        return None

    def _select_projected_team_message_id_for_append(
        self,
        session_id: str,
        *,
        role: str,
        message: dict[str, Any],
        participant_id: str,
        metadata: dict[str, Any],
    ) -> int | None:
        if str(message.get("conversation_message_id") or "").strip() or role not in {"assistant", "tool"}:
            return None
        run_id = _metadata_text(metadata, "run_id", "runId")
        if not run_id:
            return None
        turn_id = _metadata_text(metadata, "turn_id", "turnId")
        rows = self._conn.execute(
            f"SELECT {_conversation_message_columns()} "
            "FROM messages "
            "WHERE session_id = ? "
            "  AND role = ? "
            "  AND active = 1 "
            "  AND COALESCE(conversation_message_id, '') != '' "
            "ORDER BY id DESC "
            "LIMIT 128",
            (session_id, role),
        ).fetchall()
        for row in rows:
            existing_metadata = _json_or(row["metadata_json"], {})
            if not isinstance(existing_metadata, dict):
                continue
            if not (
                existing_metadata.get("team_mission")
                or existing_metadata.get("teamMission")
                or existing_metadata.get("transcript_activity_kind")
                or existing_metadata.get("transcriptActivityKind")
            ):
                continue
            if _metadata_text(existing_metadata, "run_id", "runId") != run_id:
                continue
            existing_turn_id = _metadata_text(existing_metadata, "turn_id", "turnId")
            if turn_id and existing_turn_id and existing_turn_id != turn_id:
                continue
            existing_participant_id = str(row["participant_id"] or "").strip()
            if participant_id and existing_participant_id and existing_participant_id != participant_id:
                continue
            if _decode_content(row["content"]) != message.get("content"):
                continue
            return int(row["id"])
        return None

    def _merge_projected_message(
        self,
        message_id: int,
        *,
        participant_id: str,
        metadata: dict[str, Any],
        reasoning: str,
    ) -> None:
        row = self._conn.execute(
            "SELECT participant_id, metadata_json, reasoning FROM messages WHERE id = ?",
            (int(message_id),),
        ).fetchone()
        if row is None:
            return
        existing_metadata = _json_or(row["metadata_json"], {})
        merged_metadata = _merge_metadata(
            existing_metadata if isinstance(existing_metadata, dict) else {},
            metadata,
        )
        self._conn.execute(
            """
            UPDATE messages
               SET participant_id = ?,
                   metadata_json = ?,
                   reasoning = ?,
                   active = 1
             WHERE id = ?
            """,
            (
                participant_id or str(row["participant_id"] or ""),
                json.dumps(merged_metadata, ensure_ascii=False) if merged_metadata else None,
                reasoning or str(row["reasoning"] or ""),
                int(message_id),
            ),
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
        for source_key in ("finish_reason", "reasoning", "reasoning_content"):
            if row[source_key] is not None and row[source_key] != "":
                message[source_key] = row[source_key]
        for source_key in (
            "reasoning_details",
            "codex_reasoning_items",
            "codex_message_items",
        ):
            if row[source_key]:
                message[source_key] = _json_or(row[source_key], None)
    if row["metadata_json"]:
        metadata = _json_or(row["metadata_json"], None)
        message["metadata"] = metadata
        effect_disposition = tool_effect_from_metadata(metadata)
        if effect_disposition:
            message["effect_disposition"] = effect_disposition
    return message


def _row_to_message(row: Any) -> Message:
    def _get(name: str, index: int) -> Any:
        return row[name] if isinstance(row, sqlite3.Row) else row[index]

    metadata_raw = _get("metadata_json", 12) or ""
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return Message(
        id=int(_get("id", 0) or 0),
        session_id=str(_get("session_id", 1) or ""),
        role=str(_get("role", 2) or ""),
        content=str(_get("content", 3) or ""),
        participant_id=str(_get("participant_id", 4) or ""),
        tool_call_id=str(_get("tool_call_id", 5) or ""),
        tool_calls=str(_get("tool_calls", 6) or ""),
        tool_name=str(_get("tool_name", 7) or ""),
        effect_disposition=tool_effect_from_metadata(metadata),
        timestamp=float(_get("timestamp", 8) or 0),
        reasoning=str(_get("reasoning", 9) or ""),
        conversation_message_id=str(_get("conversation_message_id", 10) or ""),
        platform_message_id=str(_get("platform_message_id", 11) or ""),
        metadata=metadata,
        active=bool(int(_get("active", 13) or 0)),
    )


def _encode_content(content: Any) -> Any:
    return encode_message_content(content)


def _decode_content(content: Any) -> Any:
    return decode_message_content(content)


def _row_metadata(row: Any) -> dict[str, Any]:
    return _json_or(row["metadata_json"], {}) if row["metadata_json"] else {}


def _merge_metadata(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    base = dict(current) if isinstance(current, dict) else {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge_metadata(base[key], value)
        else:
            base[key] = value
    return base


def _metadata_matches(
    metadata: dict[str, Any],
    *,
    run_id: str,
    turn_id: str,
    client_message_id: str,
) -> bool:
    return any(
        expected and str(metadata.get(key) or "").strip() == expected
        for key, expected in (
            ("run_id", run_id),
            ("turn_id", turn_id),
            ("client_message_id", client_message_id),
        )
    )


def _metadata_text(meta: dict[str, Any], *keys: str) -> str:
    if not isinstance(meta, dict):
        return ""
    for key in keys:
        if key not in meta:
            continue
        value = meta.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _conversation_message_columns() -> str:
    return (
        "id, session_id, role, content, participant_id, tool_call_id, tool_calls, tool_name, timestamp, "
        "finish_reason, reasoning, reasoning_content, reasoning_details, "
        "codex_reasoning_items, codex_message_items, platform_message_id, conversation_message_id, metadata_json"
    )


def _message_timestamp(message: dict[str, Any], fallback: float) -> float:
    try:
        return float(message.get("timestamp"))
    except (TypeError, ValueError):
        return fallback


def _platform_message_id(message: dict[str, Any]) -> str:
    explicit = str(message.get("platform_message_id") or "").strip()
    if explicit:
        return explicit
    candidate = str(message.get("message_id") or "").strip()
    if candidate and not (candidate.isdigit() and message.get("timestamp") is not None):
        return candidate
    return ""


def _message_preview_text(content: Any, limit: int = 60) -> str:
    preview = _plain_content_text(content, fallback="[multimodal content]")
    return preview[:limit] + "..." if len(preview) > limit else preview


def _message_display_title_text(content: Any, limit: int = 100) -> str:
    title = _plain_content_text(content, fallback="[multimodal content]")
    return title[:limit].rstrip() if len(title) > limit else title


def _tool_call_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            decoded = _decode_content(value)
        if isinstance(decoded, list):
            return len(decoded)
    return 1 if value else 0


def _plain_content_text(content: Any, *, fallback: str) -> str:
    decoded = _decode_content(content)
    if isinstance(decoded, list):
        parts = [
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item or "")
            for item in decoded
        ]
        text = " ".join(part for part in parts if part).strip()
        if not text and decoded:
            text = fallback
    elif isinstance(decoded, dict):
        text = str(decoded.get("text") or decoded.get("content") or "").strip()
    else:
        text = str(decoded or "").strip()
    return " ".join(text.split())


def _json_or_none(value: Any) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_or(value: Any, default: Any) -> Any:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return default
    return decoded if decoded is not None else default


__all__ = [
    "Message",
    "MessagePage",
    "MessageRepo",
    "MessageRepoImpl",
    "MessageRepository",
    "MessageSpec",
    "PageDirection",
]
