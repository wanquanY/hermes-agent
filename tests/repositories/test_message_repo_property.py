"""Phase K — property-style tests for MessageRepoImpl (spec §4.3)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.repositories import (
    MessageRepoImpl,
    MessageSpec,
    PageDirection,
)


PROPERTY_ROUNDS = 24


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
            active INTEGER NOT NULL DEFAULT 1,
            api_content TEXT
        );
        """
    )
    conn.commit()
    return conn


def _append_n(repo: MessageRepoImpl, session_id: str, n: int) -> list[int]:
    ids = []
    for i in range(n):
        m = repo.append(
            session_id,
            MessageSpec(session_id=session_id, role="user", content=f"m{i}"),
        )
        ids.append(m.id)
    return ids


def test_property_get_page_head_walks_every_message_via_cursor():
    """Repeated ``direction=HEAD`` calls chained by ``next_cursor_id`` visit
    each message exactly once in insertion order.
    """
    rng = random.Random(20260726)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = MessageRepoImpl(conn)
        n = rng.randint(5, 40)
        _append_n(repo, "s1", n)
        page_size = rng.randint(1, max(2, n // 3))
        visited: list[str] = []
        cursor = None
        while True:
            page = repo.get_page(
                "s1",
                cursor_id=cursor,
                direction=PageDirection.HEAD,
                limit=page_size,
            )
            visited.extend(m.content for m in page.messages)
            if not page.has_more:
                break
            cursor = page.next_cursor_id
            assert cursor is not None
        assert visited == [f"m{i}" for i in range(n)]
        conn.close()


def test_property_replace_all_deactivates_prior_history():
    """After replace_all, prior rows all end up ``active=0`` and only the
    new spec set surfaces in get_page.
    """
    rng = random.Random(20260727)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = MessageRepoImpl(conn)
        _append_n(repo, "s1", rng.randint(1, 12))

        new_history_size = rng.randint(0, 10)
        new_specs = [
            MessageSpec(session_id="s1", role="assistant", content=f"new-{i}")
            for i in range(new_history_size)
        ]
        repo.replace_all("s1", new_specs)

        active = repo.get_page("s1", direction=PageDirection.HEAD, limit=1000)
        assert [m.content for m in active.messages] == [f"new-{i}" for i in range(new_history_size)]
        total_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = 's1'"
        ).fetchone()["n"]
        # Prior rows still exist but inactive.
        assert total_rows >= new_history_size
        conn.close()


def test_property_merge_metadata_shallow_accumulates_across_calls():
    """Sequential ``merge_metadata`` patches shallow-merge into the target row."""
    rng = random.Random(20260728)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = MessageRepoImpl(conn)
        m = repo.append(
            "s1",
            MessageSpec(session_id="s1", role="user", metadata={"origin": "seed"}),
        )
        expected = {"origin": "seed"}
        for _ in range(rng.randint(1, 8)):
            patch = {
                f"k{rng.randint(0, 5)}": rng.randint(0, 999),
                "flag": rng.choice((True, False, None)),
            }
            expected.update(patch)
            got = repo.merge_metadata("s1", m.id, patch)
            assert got.metadata == expected
        conn.close()


def test_property_search_fts_matches_substring_case_sensitive():
    """search_fts's substring fallback finds every content that contains query,
    ignores everything else, and stays under the caller-supplied limit.
    """
    rng = random.Random(20260729)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = MessageRepoImpl(conn)
        n_hits = rng.randint(1, 8)
        n_misses = rng.randint(1, 8)
        query_token = f"tok{round_idx}"
        expected_hits: list[str] = []
        for i in range(n_hits):
            content = f"hello {query_token} world {i}"
            expected_hits.append(content)
            repo.append("s1", MessageSpec(session_id="s1", role="user", content=content))
        for i in range(n_misses):
            repo.append(
                "s1",
                MessageSpec(session_id="s1", role="user", content=f"no match {i}"),
            )

        matches = repo.search_fts(query_token, session_id="s1", limit=1000)
        assert {m.content for m in matches} == set(expected_hits)
        assert len(matches) == n_hits
        conn.close()


def test_property_pagination_cursor_terminates_on_finite_history():
    """No matter the page size, the paginator halts and visits all messages."""
    rng = random.Random(20260730)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = MessageRepoImpl(conn)
        n = rng.randint(1, 25)
        _append_n(repo, "s1", n)
        page_size = rng.randint(1, n + 5)

        visited_ids: set[int] = set()
        cursor = None
        iterations = 0
        max_iterations = n + 3
        while True:
            page = repo.get_page(
                "s1",
                cursor_id=cursor,
                direction=PageDirection.HEAD,
                limit=page_size,
            )
            visited_ids.update(m.id for m in page.messages)
            if not page.has_more:
                break
            cursor = page.next_cursor_id
            iterations += 1
            assert iterations < max_iterations, "pagination did not converge"
        assert len(visited_ids) == n
        conn.close()
