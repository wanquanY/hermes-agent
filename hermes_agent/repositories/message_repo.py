"""MessageRepo protocol + concrete impl (spec §4.3) — messages aggregate root."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from hermes_agent.repositories.base import RepositoryConnection


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


class MessageRepoImpl:
    """SQLite-backed MessageRepo (spec §4.3)."""

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------

    def append(self, session_id: str, message: MessageSpec) -> Message:
        stable_sid = str(session_id or "").strip()
        role = str(message.role or "").strip()
        if not stable_sid or not role:
            raise ValueError("session_id and role are required")
        ts = float(message.timestamp or time.time())
        metadata_json = json.dumps(message.metadata or {}, ensure_ascii=False)
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
        message_id = int(last_row)
        got = self._fetch_by_id(message_id)
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
            if direction is PageDirection.TAIL:
                clauses.append("id < ?")
            else:
                clauses.append("id > ?")
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
        params.append(limit + 1)  # peek one extra for has_more
        rows = self._conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        messages = [_row_to_message(r) for r in rows]
        if direction is PageDirection.TAIL:
            # TAIL returns newest first; reverse so caller can render chronologically.
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
        """Substring search fallback — FTS wiring lands with Phase D4 tests once
        the messages_fts virtual tables are present in the harness. Substring
        keeps this repo useful in isolation.
        """
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
        return [_row_to_message(r) for r in rows]

    def replace_all(self, session_id: str, history: list[MessageSpec]) -> None:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            raise ValueError("session_id is required")
        self._conn.execute(
            "UPDATE messages SET active = 0 WHERE session_id = ?", (stable_sid,)
        )
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

    # ------------------------------------------------------------------

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


def _row_to_message(row: Any) -> Message:
    def _g(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    metadata_raw = _g("metadata_json", 12) or ""
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return Message(
        id=int(_g("id", 0) or 0),
        session_id=str(_g("session_id", 1) or ""),
        role=str(_g("role", 2) or ""),
        content=str(_g("content", 3) or ""),
        participant_id=str(_g("participant_id", 4) or ""),
        tool_call_id=str(_g("tool_call_id", 5) or ""),
        tool_calls=str(_g("tool_calls", 6) or ""),
        tool_name=str(_g("tool_name", 7) or ""),
        timestamp=float(_g("timestamp", 8) or 0),
        reasoning=str(_g("reasoning", 9) or ""),
        conversation_message_id=str(_g("conversation_message_id", 10) or ""),
        platform_message_id=str(_g("platform_message_id", 11) or ""),
        metadata=metadata,
        active=bool(int(_g("active", 13) or 0)),
    )


__all__ = [
    "Message",
    "MessagePage",
    "MessageRepo",
    "MessageRepoImpl",
    "MessageSpec",
    "PageDirection",
]
