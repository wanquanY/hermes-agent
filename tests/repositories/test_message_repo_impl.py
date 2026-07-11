"""Phase D4 — MessageRepoImpl concrete behavior (spec §4.3)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.repositories import (
    Message,
    MessagePage,
    MessageRepository,
    MessageRepo,
    MessageRepoImpl,
    MessageSpec,
    PageDirection,
    SessionRepoImpl,
    SessionSpec,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
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
            conversation_message_id TEXT NOT NULL DEFAULT '',
            platform_message_id TEXT,
            metadata_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()
    return conn


def _make_conversation_conn() -> sqlite3.Connection:
    conn = _make_conn()
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            title TEXT,
            display_title TEXT,
            display_title_source TEXT,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_active REAL,
            ended_at REAL,
            transient INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            preview TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY,
            owner_agent_profile_id TEXT NOT NULL DEFAULT '',
            owner_profile_version_id TEXT NOT NULL DEFAULT '',
            runtime_scope_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'unknown',
            transient INTEGER NOT NULL DEFAULT 0,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            message_count INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            last_activity REAL
        );
        """
    )
    conn.commit()
    return conn


def test_impl_is_structural_message_repo():
    repo = MessageRepoImpl(_make_conn())
    assert isinstance(repo, MessageRepo)


def test_append_persists_message_and_returns_projection():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    msg = repo.append(
        "s1",
        MessageSpec(session_id="s1", role="user", content="hello", timestamp=1.0),
    )
    assert isinstance(msg, Message)
    assert msg.session_id == "s1"
    assert msg.role == "user"
    assert msg.content == "hello"
    assert msg.active is True

    row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    assert row["n"] == 1


def test_append_requires_session_and_role():
    repo = MessageRepoImpl(_make_conn())
    with pytest.raises(ValueError):
        repo.append("", MessageSpec(session_id="", role="user"))
    with pytest.raises(ValueError):
        repo.append("s1", MessageSpec(session_id="s1", role=""))


def test_get_page_tail_returns_newest_last():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    for i in range(5):
        repo.append("s1", MessageSpec(session_id="s1", role="user", content=f"m{i}", timestamp=float(i)))
    page = repo.get_page("s1", direction=PageDirection.TAIL, limit=10)
    assert isinstance(page, MessagePage)
    assert [m.content for m in page.messages] == ["m0", "m1", "m2", "m3", "m4"]
    assert page.has_more is False


def test_get_page_tail_pagination_via_cursor():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    for i in range(20):
        repo.append("s1", MessageSpec(session_id="s1", role="user", content=f"m{i}"))
    first = repo.get_page("s1", direction=PageDirection.TAIL, limit=5)
    assert len(first.messages) == 5
    assert [m.content for m in first.messages] == ["m15", "m16", "m17", "m18", "m19"]
    assert first.has_more is True
    assert first.next_cursor_id is not None

    second = repo.get_page(
        "s1",
        direction=PageDirection.TAIL,
        limit=5,
        cursor_id=first.next_cursor_id,
    )
    assert [m.content for m in second.messages] == ["m10", "m11", "m12", "m13", "m14"]


def test_get_page_head_returns_oldest_first():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    for i in range(3):
        repo.append("s1", MessageSpec(session_id="s1", role="user", content=f"m{i}"))
    page = repo.get_page("s1", direction=PageDirection.HEAD, limit=10)
    assert [m.content for m in page.messages] == ["m0", "m1", "m2"]


def test_search_fts_substring_fallback():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="hello world"))
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="goodbye"))
    got = repo.search_fts("hello", session_id="s1")
    assert [m.content for m in got] == ["hello world"]


def test_search_fts_empty_query_returns_empty():
    repo = MessageRepoImpl(_make_conn())
    assert repo.search_fts("") == []


def test_replace_all_deactivates_previous_history():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="old-1"))
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="old-2"))

    repo.replace_all(
        "s1",
        [
            MessageSpec(session_id="s1", role="user", content="new-1"),
            MessageSpec(session_id="s1", role="assistant", content="new-2"),
        ],
    )

    active = repo.get_page("s1", direction=PageDirection.HEAD, limit=10)
    assert [m.content for m in active.messages] == ["new-1", "new-2"]

    total = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert total == 4  # 2 old (inactive) + 2 new (active)


def test_merge_metadata_shallow_merge():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    msg = repo.append(
        "s1",
        MessageSpec(session_id="s1", role="user", content="hi", metadata={"seq": 1}),
    )
    updated = repo.merge_metadata("s1", msg.id, {"stripped_speaker_prefix": True})
    assert updated.metadata == {"seq": 1, "stripped_speaker_prefix": True}


def test_merge_metadata_missing_message_raises():
    repo = MessageRepoImpl(_make_conn())
    with pytest.raises(LookupError):
        repo.merge_metadata("s1", 9999, {"x": 1})


def test_delete_by_session_removes_only_target_messages():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    repo.append("s1", MessageSpec(session_id="s1", role="user", content="one"))
    repo.append("s1", MessageSpec(session_id="s1", role="assistant", content="two"))
    repo.append("s2", MessageSpec(session_id="s2", role="user", content="keep"))

    assert repo.delete_by_session("s1") == 2

    rows = conn.execute("SELECT session_id, content FROM messages ORDER BY id").fetchall()
    assert [(row["session_id"], row["content"]) for row in rows] == [("s2", "keep")]


def test_deactivate_member_chat_view_sources_only_deactivates_matching_view_rows():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    repo.append(
        "memberchat:s1:m1",
        MessageSpec(
            session_id="memberchat:s1:m1",
            role="user",
            content="target spaced",
            metadata={"member_chat_view": {"source_message_id": "10"}},
        ),
    )
    repo.append(
        "memberchat:s1:m1",
        MessageSpec(
            session_id="memberchat:s1:m1",
            role="assistant",
            content="target compact",
            metadata={"member_chat_view": {"source_message_id": "11"}},
        ),
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, role, content, participant_id, timestamp, metadata_json, active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (
            "memberchat:s1:m1",
            "user",
            "manual compact",
            "",
            1.0,
            '{"member_chat_view":{"source_message_id":"12"}}',
        ),
    )
    repo.append(
        "memberchat:s1:m1",
        MessageSpec(
            session_id="memberchat:s1:m1",
            role="user",
            content="different source",
            metadata={"member_chat_view": {"source_message_id": "99"}},
        ),
    )
    repo.append(
        "memberchat:other:m1",
        MessageSpec(
            session_id="memberchat:other:m1",
            role="user",
            content="same source different session",
            metadata={"member_chat_view": {"source_message_id": "10"}},
        ),
    )

    assert repo.deactivate_member_chat_view_sources("memberchat:s1:m1", [10, 12]) == 2

    rows = conn.execute(
        """
        SELECT session_id, content, active
          FROM messages
         ORDER BY id
        """
    ).fetchall()
    assert [(row["session_id"], row["content"], row["active"]) for row in rows] == [
        ("memberchat:s1:m1", "target spaced", 0),
        ("memberchat:s1:m1", "target compact", 1),
        ("memberchat:s1:m1", "manual compact", 0),
        ("memberchat:s1:m1", "different source", 1),
        ("memberchat:other:m1", "same source different session", 1),
    ]


def test_copy_branch_prefix_materializes_ordered_messages_to_target_session():
    conn = _make_conn()
    repo = MessageRepoImpl(conn)
    first = repo.append(
        "source",
        MessageSpec(
            session_id="source",
            role="user",
            content="one",
            timestamp=1.0,
            metadata={"seq": 1},
        ),
    )
    second = repo.append(
        "source",
        MessageSpec(
            session_id="source",
            role="assistant",
            content="two",
            tool_calls='[{"name":"search"}]',
            tool_name="search",
            reasoning="thinking",
            platform_message_id="platform-2",
            timestamp=2.0,
        ),
    )
    repo.append(
        "source",
        MessageSpec(session_id="source", role="assistant", content="after", timestamp=3.0),
    )
    repo.append(
        "other",
        MessageSpec(session_id="other", role="user", content="ignored", timestamp=0.5),
    )

    assert repo.copy_branch_prefix(["source"], second.id, "branch", 100.0) == 2

    rows = conn.execute(
        """
        SELECT session_id, role, content, tool_calls, tool_name, reasoning,
               platform_message_id, metadata_json, timestamp
          FROM messages
         WHERE session_id = 'branch'
         ORDER BY id
        """
    ).fetchall()
    assert [row["content"] for row in rows] == ["one", "two"]
    assert rows[0]["metadata_json"] == '{"seq": 1}'
    assert rows[1]["tool_calls"] == '[{"name":"search"}]'
    assert rows[1]["tool_name"] == "search"
    assert rows[1]["reasoning"] == "thinking"
    assert rows[1]["platform_message_id"] == "platform-2"
    assert [row["timestamp"] for row in rows] == [100.000001, 100.000002]
    assert first.id < second.id


def test_conversation_message_append_updates_session_projection_via_session_repo():
    conn = _make_conversation_conn()
    sessions = SessionRepoImpl(conn)
    sessions.create(SessionSpec(session_id="s1", source="test", title="Original"))
    conn.commit()
    repo = MessageRepository(conn, sessions)

    message_id = repo.append_conversation_message(
        "s1",
        {
            "role": "user",
            "content": "hello from user",
            "tool_calls": [{"id": "tool-1"}, {"id": "tool-2"}],
            "timestamp": 1000.0,
        },
    )

    assert message_id > 0
    session_row = conn.execute(
        """
        SELECT message_count, tool_call_count, preview, display_title,
               display_title_source
          FROM sessions
         WHERE id = 's1'
        """
    ).fetchone()
    assert dict(session_row) == {
        "message_count": 1,
        "tool_call_count": 2,
        "preview": "hello from user",
        "display_title": "hello from user",
        "display_title_source": "first_user_message",
    }
    index_row = conn.execute(
        "SELECT title, preview, message_count, last_activity FROM session_index WHERE session_id='s1'"
    ).fetchone()
    assert index_row["title"] == "hello from user"
    assert index_row["preview"] == "hello from user"
    assert index_row["message_count"] == 1
    assert index_row["last_activity"] == 1000.0


def test_rebuild_session_projection_restores_session_and_index_from_active_messages():
    conn = _make_conversation_conn()
    sessions = SessionRepoImpl(conn)
    sessions.create(SessionSpec(session_id="s1", source="test", title="Original"))
    conn.executemany(
        """
        INSERT INTO messages (
            session_id, role, content, tool_calls, timestamp, active
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("s1", "assistant", "ignored assistant", None, 1.0, 1),
            ("s1", "user", "first user message", None, 2.0, 1),
            ("s1", "assistant", "tool owner", '[{"name":"search"},{"name":"read"}]', 3.0, 1),
            ("s1", "user", "inactive user", None, 4.0, 0),
        ],
    )
    conn.execute(
        """
        UPDATE sessions
           SET message_count = 99,
               tool_call_count = 99,
               preview = 'stale',
               display_title = 'stale',
               display_title_source = ''
         WHERE id = 's1'
        """
    )
    conn.execute(
        """
        UPDATE session_index
           SET title = 'stale',
               preview = 'stale',
               message_count = 99,
               last_activity = 0
         WHERE session_id = 's1'
        """
    )
    conn.commit()

    MessageRepository(conn, sessions).rebuild_session_projection("s1")

    session_row = conn.execute(
        """
        SELECT message_count, tool_call_count, preview, display_title,
               display_title_source, last_active
          FROM sessions
         WHERE id = 's1'
        """
    ).fetchone()
    assert dict(session_row) == {
        "message_count": 3,
        "tool_call_count": 2,
        "preview": "first user message",
        "display_title": "first user message",
        "display_title_source": "first_user_message",
        "last_active": 3.0,
    }
    index_row = conn.execute(
        """
        SELECT title, preview, message_count, last_activity
          FROM session_index
         WHERE session_id = 's1'
        """
    ).fetchone()
    assert dict(index_row) == {
        "title": "first user message",
        "preview": "first user message",
        "message_count": 3,
        "last_activity": 3.0,
    }
