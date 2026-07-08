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
