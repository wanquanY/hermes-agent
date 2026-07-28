from __future__ import annotations

import json
import sqlite3

from hermes_agent.read_models.message_history import MessageHistoryReadModel, MessagePageQuery


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            participant_id TEXT NOT NULL DEFAULT '',
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            codex_message_items TEXT,
            platform_message_id TEXT,
            conversation_message_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            api_content TEXT
        );
        """
    )
    return conn


def _insert(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    *,
    timestamp: float,
    metadata: dict | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO messages (
            session_id, role, content, timestamp, metadata_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            session_id,
            role,
            content,
            timestamp,
            json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_page_returns_storage_cursors_and_message_projection():
    conn = _conn()
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    first = _insert(conn, "s1", "user", "old", timestamp=1.0)
    second = _insert(conn, "s1", "assistant", "new", timestamp=2.0)

    page = MessageHistoryReadModel(conn).page_as_conversation(
        "s1",
        MessagePageQuery(direction="tail", limit=1),
    )

    assert page["messages"] == [
        {
            "role": "assistant",
            "content": "new",
            "message_id": str(second),
            "timestamp": 2.0,
        }
    ]
    assert page["pageInfo"]["prev_cursor_id"] == second
    assert page["pageInfo"]["next_cursor_id"] is None
    assert page["pageInfo"]["hasMoreBefore"] is True
    assert page["pageInfo"]["hasMoreAfter"] is False
    assert page["pageInfo"]["totalCount"] == 2


def test_tool_effect_disposition_projects_from_canonical_metadata():
    conn = _conn()
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    _insert(
        conn,
        "s1",
        "tool",
        "outcome unavailable",
        timestamp=1.0,
        metadata={"_hermes_tool_effect_disposition": "unknown"},
    )

    messages = MessageHistoryReadModel(conn).all_as_conversation("s1")
    assert messages[0]["effect_disposition"] == "unknown"


def test_page_expands_selected_assistant_to_turn_user_boundary():
    conn = _conn()
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    _insert(conn, "s1", "user", "prompt", timestamp=1.0, metadata={"turn_id": "turn-1"})
    _insert(conn, "s1", "assistant", "answer", timestamp=2.0, metadata={"turn_id": "turn-1"})

    page = MessageHistoryReadModel(conn).page_as_conversation(
        "s1",
        MessagePageQuery(direction="tail", limit=1),
    )

    assert [message["content"] for message in page["messages"]] == ["prompt", "answer"]


def test_page_includes_ancestor_chain_without_duplicate_user_replay():
    conn = _conn()
    conn.execute("INSERT INTO sessions (id, parent_session_id) VALUES ('root', NULL)")
    conn.execute("INSERT INTO sessions (id, parent_session_id) VALUES ('child', 'root')")
    _insert(conn, "root", "user", "same prompt", timestamp=1.0)
    _insert(conn, "child", "user", "same prompt", timestamp=2.0)
    _insert(conn, "child", "assistant", "child answer", timestamp=3.0)

    page = MessageHistoryReadModel(conn).page_as_conversation(
        "child",
        MessagePageQuery(direction="tail", limit=10, include_ancestors=True),
    )

    assert [message["content"] for message in page["messages"]] == [
        "same prompt",
        "child answer",
    ]
