from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

from agent.memory_manager import sanitize_context

logger = logging.getLogger(__name__)


class SessionDBMessageMixin:
    # =========================================================================
    # Message storage
    # =========================================================================

    # Sentinel prefix used to distinguish JSON-encoded structured content
    # (multimodal messages: lists of parts like text + image_url) from plain
    # string content. The NUL byte is not legal in normal text, so this
    # cannot collide with real user content.
    _CONTENT_JSON_PREFIX = "\x00json:"

    @classmethod
    def _encode_content(cls, content: Any) -> Any:
        """Serialize structured (list/dict) message content for sqlite.

        sqlite3 can only bind ``str``, ``bytes``, ``int``, ``float``, and ``None``
        to query parameters. Multimodal messages have ``content`` as a list of
        parts (``[{"type": "text", ...}, {"type": "image_url", ...}]``), which
        raises ``ProgrammingError: Error binding parameter N: type 'list' is
        not supported`` when bound directly.

        Returns the value unchanged when it's already a safe scalar, or a
        sentinel-prefixed JSON string for lists/dicts. Paired with
        :meth:`_decode_content` on read.
        """
        if content is None or isinstance(content, (str, bytes, int, float)):
            return content
        try:
            return cls._CONTENT_JSON_PREFIX + json.dumps(content)
        except (TypeError, ValueError):
            # Last-resort fallback: stringify so persistence never fails.
            return str(content)

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Reverse :meth:`_encode_content`; returns scalars unchanged."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            try:
                return json.loads(content[len(cls._CONTENT_JSON_PREFIX):])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to decode JSON-encoded message content; "
                    "returning raw string"
                )
                return content
        return content

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_content: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
        metadata: Any = None,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        # Serialize structured fields to JSON before entering the write txn
        reasoning_details_json = (
            json.dumps(reasoning_details)
            if reasoning_details else None
        )
        codex_items_json = (
            json.dumps(codex_reasoning_items)
            if codex_reasoning_items else None
        )
        codex_message_items_json = (
            json.dumps(codex_message_items)
            if codex_message_items else None
        )
        metadata_json = json.dumps(metadata) if metadata else None
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        # Multimodal content (list of parts) must be JSON-encoded: sqlite3
        # cannot bind list/dict parameters directly.
        stored_content = self._encode_content(content)

        # Pre-compute tool call count
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        def _do(conn):
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason,
                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                   codex_message_items, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    stored_content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_content,
                    reasoning_details_json,
                    codex_items_json,
                    codex_message_items_json,
                    metadata_json,
                ),
            )
            msg_id = cursor.lastrowid

            # Update counters
            if num_tool_calls > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
            return msg_id

        return self._execute_write(_do)

    def replace_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Atomically replace every message for a session.

        Used by transcript-rewrite flows such as /retry, /undo, and /compress.
        The delete + reinsert sequence must commit as one transaction so a
        mid-rewrite failure does not leave SQLite with a partial transcript.
        """

        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )

            now_ts = time.time()
            total_messages = 0
            total_tool_calls = 0
            for msg in messages:
                role = msg.get("role", "unknown")
                tool_calls = msg.get("tool_calls")
                reasoning_details = msg.get("reasoning_details") if role == "assistant" else None
                codex_reasoning_items = (
                    msg.get("codex_reasoning_items") if role == "assistant" else None
                )
                codex_message_items = (
                    msg.get("codex_message_items") if role == "assistant" else None
                )
                metadata = msg.get("metadata")

                reasoning_details_json = (
                    json.dumps(reasoning_details) if reasoning_details else None
                )
                codex_items_json = (
                    json.dumps(codex_reasoning_items) if codex_reasoning_items else None
                )
                codex_message_items_json = (
                    json.dumps(codex_message_items) if codex_message_items else None
                )
                metadata_json = json.dumps(metadata) if metadata else None
                tool_calls_json = json.dumps(tool_calls) if tool_calls else None

                conn.execute(
                    """INSERT INTO messages (session_id, role, content, tool_call_id,
                       tool_calls, tool_name, timestamp, token_count, finish_reason,
                       reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                       codex_message_items, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        role,
                        self._encode_content(msg.get("content")),
                        msg.get("tool_call_id"),
                        tool_calls_json,
                        msg.get("tool_name"),
                        now_ts,
                        msg.get("token_count"),
                        msg.get("finish_reason"),
                        msg.get("reasoning") if role == "assistant" else None,
                        msg.get("reasoning_content") if role == "assistant" else None,
                        reasoning_details_json,
                        codex_items_json,
                        codex_message_items_json,
                        metadata_json,
                    ),
                )
                total_messages += 1
                if tool_calls is not None:
                    total_tool_calls += (
                        len(tool_calls) if isinstance(tool_calls, list) else 1
                    )
                now_ts += 1e-6

            conn.execute(
                "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                (total_messages, total_tool_calls, session_id),
            )

        self._execute_write(_do)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by insertion order."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in get_messages, falling back to []")
                    msg["tool_calls"] = []
            if msg.get("metadata_json"):
                try:
                    msg["metadata"] = json.loads(msg["metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize metadata_json in get_messages")
                    msg["metadata"] = None
            result.append(msg)
        return result

    def resolve_resume_session_id(self, session_id: str) -> str:
        """Redirect a resume target to the descendant session that holds the messages.

        Context compression ends the current session and forks a new child session
        (linked via ``parent_session_id``). The flush cursor is reset, so the
        child is where new messages actually land — the parent ends up with
        ``message_count = 0`` rows unless messages had already been flushed to
        it before compression. See #15000.

        This helper walks ``parent_session_id`` forward from ``session_id`` and
        returns the first descendant in the chain that has at least one message
        row. If the original session already has messages, or no descendant
        has any, the original ``session_id`` is returned unchanged.

        The chain is always walked via the child whose ``started_at`` is
        latest; that matches the single-chain shape that compression creates.
        A depth cap (32) guards against accidental loops in malformed data.
        """
        if not session_id:
            return session_id

        with self._lock:
            # If this session already has messages, nothing to redirect.
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            except Exception:
                return session_id
            if row is not None:
                return session_id

            # Walk descendants: at each step, pick the most-recently-started
                # child session; stop once we find one with messages.
            current = session_id
            seen = {current}
            for _ in range(32):
                try:
                    child_row = self._conn.execute(
                        "SELECT id FROM sessions "
                        "WHERE parent_session_id = ? "
                        "ORDER BY started_at DESC, id DESC LIMIT 1",
                        (current,),
                    ).fetchone()
                except Exception:
                    return session_id
                if child_row is None:
                    return session_id
                child_id = child_row["id"] if hasattr(child_row, "keys") else child_row[0]
                if not child_id or child_id in seen:
                    return session_id
                seen.add(child_id)
                try:
                    msg_row = self._conn.execute(
                        "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                        (child_id,),
                    ).fetchone()
                except Exception:
                    return session_id
                if msg_row is not None:
                    return child_id
                current = child_id
        return session_id

    _CONVERSATION_MESSAGE_COLUMNS = (
        "id, session_id, role, content, tool_call_id, tool_calls, tool_name, "
        "timestamp, finish_reason, reasoning, reasoning_content, reasoning_details, "
        "codex_reasoning_items, codex_message_items, metadata_json"
    )

    def _conversation_message_from_row(
        self,
        row: sqlite3.Row,
        *,
        include_storage_metadata: bool = False,
    ) -> Dict[str, Any]:
        content = self._decode_content(row["content"])
        if row["role"] in {"user", "assistant"} and isinstance(content, str):
            content = sanitize_context(content).strip()
        msg: Dict[str, Any] = {"role": row["role"], "content": content}
        if include_storage_metadata:
            msg["id"] = row["id"]
            msg["session_id"] = row["session_id"]
            msg["timestamp"] = row["timestamp"]
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        if row["tool_name"]:
            msg["tool_name"] = row["tool_name"]
        if row["tool_calls"]:
            try:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize tool_calls in conversation replay, falling back to []")
                msg["tool_calls"] = []
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
                if isinstance(metadata, dict):
                    msg["metadata"] = metadata
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize metadata_json in conversation replay")
        # Restore reasoning fields on assistant messages so providers that
        # replay reasoning (OpenRouter, OpenAI, Nous) receive coherent
        # multi-turn reasoning context.
        if row["role"] == "assistant":
            if row["finish_reason"]:
                msg["finish_reason"] = row["finish_reason"]
            if row["reasoning"]:
                msg["reasoning"] = row["reasoning"]
            if row["reasoning_content"] is not None:
                msg["reasoning_content"] = row["reasoning_content"]
            if row["reasoning_details"]:
                try:
                    msg["reasoning_details"] = json.loads(row["reasoning_details"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize reasoning_details, falling back to None")
                    msg["reasoning_details"] = None
            if row["codex_reasoning_items"]:
                try:
                    msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize codex_reasoning_items, falling back to None")
                    msg["codex_reasoning_items"] = None
            if row["codex_message_items"]:
                try:
                    msg["codex_message_items"] = json.loads(row["codex_message_items"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize codex_message_items, falling back to None")
                    msg["codex_message_items"] = None
        return msg

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(session_id)

        with self._lock:
            placeholders = ",".join("?" for _ in session_ids)
            rows = self._conn.execute(
                f"SELECT {self._CONVERSATION_MESSAGE_COLUMNS} "
                f"FROM messages WHERE session_id IN ({placeholders}) ORDER BY id",
                tuple(session_ids),
            ).fetchall()

        messages = []
        for row in rows:
            msg = self._conversation_message_from_row(
                row,
                include_storage_metadata=include_storage_metadata,
            )
            if include_ancestors and self._is_duplicate_replayed_user_message(messages, msg):
                continue
            messages.append(msg)
        return messages

    def get_messages_page_as_conversation(
        self,
        session_id: str,
        *,
        direction: str = "tail",
        cursor_id: Optional[int] = None,
        limit: int = 50,
        include_ancestors: bool = False,
    ) -> Dict[str, Any]:
        """Load one UI transcript page with stable storage cursors.

        This is intentionally separate from ``get_messages_as_conversation``:
        runtime replay still receives the complete OpenAI-style history, while
        UI clients can hydrate only the visible tail and request older pages on
        demand.
        """
        normalized_direction = direction if direction in {"tail", "before", "after"} else "tail"
        page_limit = max(1, min(int(limit or 50), 200))
        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(session_id)

        placeholders = ",".join("?" for _ in session_ids)
        clauses = [f"session_id IN ({placeholders})"]
        params: List[Any] = list(session_ids)
        order_by = "id DESC"
        if normalized_direction == "before" and cursor_id is not None:
            clauses.append("id < ?")
            params.append(int(cursor_id))
            order_by = "id DESC"
        elif normalized_direction == "after" and cursor_id is not None:
            clauses.append("id > ?")
            params.append(int(cursor_id))
            order_by = "id ASC"

        where_sql = " AND ".join(clauses)
        with self._lock:
            total_row = self._conn.execute(
                f"SELECT COUNT(*) AS count FROM messages WHERE session_id IN ({placeholders})",
                tuple(session_ids),
            ).fetchone()
            rows = self._conn.execute(
                f"SELECT {self._CONVERSATION_MESSAGE_COLUMNS} "
                f"FROM messages WHERE {where_sql} ORDER BY {order_by} LIMIT ?",
                tuple(params + [page_limit + 1]),
            ).fetchall()

        has_extra = len(rows) > page_limit
        page_rows = rows[:page_limit]
        if normalized_direction in {"tail", "before"}:
            page_rows = list(reversed(page_rows))

        messages: List[Dict[str, Any]] = []
        for row in page_rows:
            msg = self._conversation_message_from_row(row, include_storage_metadata=True)
            if include_ancestors and self._is_duplicate_replayed_user_message(messages, msg):
                continue
            messages.append(msg)

        first_id = page_rows[0]["id"] if page_rows else None
        last_id = page_rows[-1]["id"] if page_rows else None
        if normalized_direction == "after":
            has_more_before = cursor_id is not None
            has_more_after = has_extra
        elif normalized_direction == "before":
            has_more_before = has_extra
            has_more_after = cursor_id is not None
        else:
            has_more_before = has_extra
            has_more_after = False

        return {
            "messages": messages,
            "pageInfo": {
                "prev_cursor_id": first_id,
                "next_cursor_id": last_id,
                "hasMoreBefore": bool(has_more_before),
                "hasMoreAfter": bool(has_more_after),
                "totalCount": int(total_row["count"] if total_row else 0),
            },
        }

    def _session_lineage_root_to_tip(self, session_id: str) -> List[str]:
        if not session_id:
            return [session_id]

        chain = []
        current = session_id
        seen = set()
        with self._lock:
            for _ in range(100):
                if not current or current in seen:
                    break
                seen.add(current)
                chain.append(current)
                row = self._conn.execute(
                    "SELECT parent_session_id FROM sessions WHERE id = ?",
                    (current,),
                ).fetchone()
                if row is None:
                    break
                current = row["parent_session_id"] if hasattr(row, "keys") else row[0]
        return list(reversed(chain)) or [session_id]

    @staticmethod
    def _is_duplicate_replayed_user_message(messages: List[Dict[str, Any]], msg: Dict[str, Any]) -> bool:
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get("role") == "user" and prev.get("content") == content:
                return True
            if prev.get("role") == "assistant" and (prev.get("content") or prev.get("tool_calls")):
                return False
        return False

