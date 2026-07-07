"""Phase D4 — MessageRepoImpl concrete behavior (spec §4.3)."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_agent.repositories import (
    Message,
    MessagePage,
    MessageRepo,
    MessageRepoImpl,
    MessageSpec,
    PageDirection,
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
            reasoning TEXT,
            conversation_message_id TEXT NOT NULL DEFAULT '',
            platform_message_id TEXT,
            metadata_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
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
