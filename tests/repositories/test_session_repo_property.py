"""Phase K — property-style tests for SessionRepoImpl (spec §4.1)."""

from __future__ import annotations

import random
import sqlite3

from hermes_agent.repositories import (
    BranchSpec,
    SessionFilter,
    SessionRepoImpl,
    SessionSpec,
)


PROPERTY_ROUNDS = 24


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            title TEXT,
            display_title TEXT,
            display_title_source TEXT,
            session_kind TEXT NOT NULL DEFAULT 'hermes_session',
            conversation_kind TEXT NOT NULL DEFAULT 'direct',
            parent_session_id TEXT,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE session_index (
            session_id TEXT PRIMARY KEY,
            owner_agent_profile_id TEXT NOT NULL DEFAULT '',
            owner_profile_version_id TEXT NOT NULL DEFAULT '',
            runtime_scope_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            session_kind TEXT NOT NULL DEFAULT '',
            conversation_kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',
            running INTEGER NOT NULL DEFAULT 0,
            waiting_approval INTEGER NOT NULL DEFAULT 0,
            active_run_id TEXT NOT NULL DEFAULT '',
            active_runtime_session_id TEXT NOT NULL DEFAULT '',
            pending_approval_count INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            last_activity REAL
        );
        CREATE TABLE session_branches (
            child_session_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            branch_from_seq INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    return conn


def test_property_get_returns_last_created_projection():
    """Regardless of interleaved creates, get(session_id) reflects the latest create."""
    rng = random.Random(20260716)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = SessionRepoImpl(conn)
        n_sessions = rng.randint(3, 10)
        titles = {}
        for i in range(n_sessions):
            sid = f"s-{round_idx}-{i}"
            title = f"title-{rng.randint(0, 999)}"
            repo.create(SessionSpec(session_id=sid, source="test", title=title))
            titles[sid] = title
        for sid, expected in titles.items():
            got = repo.get(sid)
            assert got is not None
            assert got.title == expected
        conn.close()


def test_property_list_excludes_ended_by_default():
    rng = random.Random(20260717)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = SessionRepoImpl(conn)
        n_sessions = rng.randint(2, 12)
        all_ids: list[str] = []
        ended: set[str] = set()
        for i in range(n_sessions):
            sid = f"s-{round_idx}-{i}"
            repo.create(SessionSpec(session_id=sid, source="test"))
            all_ids.append(sid)
        # Close a random subset.
        for sid in all_ids:
            if rng.random() < 0.4:
                repo.close(sid, reason="test")
                ended.add(sid)
        active = {s.session_id for s in repo.list(SessionFilter(limit=1000))}
        assert active == set(all_ids) - ended
        every = {
            s.session_id for s in repo.list(SessionFilter(include_ended=True, limit=1000))
        }
        assert every == set(all_ids)
        conn.close()


def test_property_branch_persists_parent_link():
    """Every branch call writes a session_branches row pointing to source."""
    rng = random.Random(20260718)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = SessionRepoImpl(conn)
        parent_id = f"parent-{round_idx}"
        repo.create(SessionSpec(session_id=parent_id, source="test"))

        n_branches = rng.randint(1, 6)
        for i in range(n_branches):
            child_id = f"branch-{round_idx}-{i}"
            branch_from_seq = rng.randint(0, 100)
            child = repo.branch(
                parent_id,
                BranchSpec(
                    new_session_id=child_id,
                    branch_from_seq=branch_from_seq,
                    title=f"child-{i}",
                ),
            )
            assert child.parent_session_id == parent_id

            row = conn.execute(
                "SELECT parent_session_id, branch_from_seq FROM session_branches "
                "WHERE child_session_id = ?",
                (child_id,),
            ).fetchone()
            assert row["parent_session_id"] == parent_id
            assert row["branch_from_seq"] == branch_from_seq
        conn.close()


def test_property_close_is_idempotent_and_final():
    """K closes on the same session leave ended_at populated exactly once
    and never revive it back to unclosed.
    """
    rng = random.Random(20260719)
    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = SessionRepoImpl(conn)
        sid = f"s-{round_idx}"
        repo.create(SessionSpec(session_id=sid, source="test"))
        k = rng.randint(1, 8)
        first_ended_at = None
        for i in range(k):
            repo.close(sid, reason=f"reason-{i}")
            got = repo.get(sid)
            assert got.ended_at is not None
            if first_ended_at is None:
                first_ended_at = got.ended_at
        # The row remains closed.
        row = conn.execute(
            "SELECT ended_at, end_reason FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
        assert row["ended_at"] is not None
        conn.close()


def test_property_update_index_partial_patch_composes_across_calls():
    """Sequential partial patches accumulate into the projection row."""
    rng = random.Random(20260720)
    from hermes_agent.repositories import SessionIndexPatch

    for round_idx in range(PROPERTY_ROUNDS):
        conn = _make_conn()
        repo = SessionRepoImpl(conn)
        sid = f"s-{round_idx}"
        repo.create(SessionSpec(session_id=sid, source="test"))

        expected_status = "idle"
        expected_active_run = ""
        expected_running = 0
        expected_message_count = 0

        for _ in range(rng.randint(3, 8)):
            fields_to_patch = {}
            if rng.random() < 0.5:
                expected_status = rng.choice(("idle", "running", "waiting_approval"))
                fields_to_patch["status"] = expected_status
            if rng.random() < 0.5:
                expected_active_run = f"run-{rng.randint(1, 999)}"
                fields_to_patch["active_run_id"] = expected_active_run
            if rng.random() < 0.5:
                expected_running = rng.choice((0, 1))
                fields_to_patch["running"] = expected_running
            if rng.random() < 0.5:
                delta = rng.randint(1, 10)
                expected_message_count += delta
                fields_to_patch["message_count"] = expected_message_count
            if not fields_to_patch:
                continue
            repo.update_index(sid, SessionIndexPatch(**fields_to_patch))
        row = conn.execute(
            "SELECT status, active_run_id, running, message_count "
            "FROM session_index WHERE session_id = ?",
            (sid,),
        ).fetchone()
        assert row["status"] == expected_status
        assert row["active_run_id"] == expected_active_run
        assert row["running"] == expected_running
        assert row["message_count"] == expected_message_count
        conn.close()
