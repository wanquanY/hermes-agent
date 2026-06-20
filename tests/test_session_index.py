from pathlib import Path

from hermes_state import SessionDB


def _ids(result):
    return [s["session_id"] for s in result["sessions"]]


def test_upsert_and_list_roundtrip(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(
        session_id="s1",
        owner_agent_profile_id="agent-7",
        title="Hello",
        preview="hi there",
        source="cli",
        running=True,
        status="running",
        active_run_id="run-1",
        started_at=100.0,
        updated_at=100.0,
    )
    result = db.list_session_index()
    assert _ids(result) == ["s1"]
    item = result["sessions"][0]
    assert item["owner_agent_profile_id"] == "agent-7"
    assert item["title"] == "Hello"
    assert item["running"] is True  # cast to bool
    assert item["transient"] is False
    assert item["status"] == "running"
    assert item["active_run_id"] == "run-1"


def test_upsert_is_idempotent_by_id(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(session_id="s1", title="v1", started_at=1.0, updated_at=1.0)
    db.upsert_session_index(session_id="s1", title="v2", running=True, started_at=1.0, updated_at=2.0)
    result = db.list_session_index()
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["title"] == "v2"
    assert result["sessions"][0]["running"] is True


def test_transient_excluded_by_default(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(session_id="real", started_at=1.0, updated_at=1.0)
    db.upsert_session_index(session_id="draft", transient=True, started_at=2.0, updated_at=2.0)
    assert _ids(db.list_session_index()) == ["real"]
    assert set(_ids(db.list_session_index(include_transient=True))) == {"real", "draft"}


def test_ordering_newest_first(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(session_id="old", started_at=1.0, updated_at=1.0)
    db.upsert_session_index(session_id="new", started_at=3.0, updated_at=3.0)
    db.upsert_session_index(session_id="mid", started_at=2.0, updated_at=2.0)
    assert _ids(db.list_session_index()) == ["new", "mid", "old"]


def test_keyset_pagination_no_dupes_full_coverage(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    for i in range(10):
        db.upsert_session_index(session_id=f"s{i:02d}", started_at=float(i), updated_at=float(i))
    seen = []
    cursor = None
    for _ in range(10):
        page = db.list_session_index(limit=3, cursor=cursor)
        seen.extend(_ids(page))
        cursor = page["pageInfo"]["nextCursor"]
        if not page["pageInfo"]["hasMore"]:
            break
    assert len(seen) == 10
    assert len(set(seen)) == 10  # no duplicates across pages
    assert seen == sorted(seen, reverse=True)  # newest-first preserved


def test_delete_removes_row(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(session_id="s1", started_at=1.0, updated_at=1.0)
    assert db.delete_session_index("s1") == 1
    assert db.list_session_index()["sessions"] == []


def test_reconcile_backfills_from_sessions_excluding_tool(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv-1", source="cli")
    db.create_session("tool-1", source="tool")
    result = db.reconcile_session_index()
    assert result["reconciled"] >= 1
    ids = _ids(db.list_session_index())
    assert "conv-1" in ids
    assert "tool-1" not in ids  # tool source excluded


def test_reconcile_preserves_live_status(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv-1", source="cli")
    db.upsert_session_index(
        session_id="conv-1", title="Conversation 1", source="cli",
        running=True, status="running", active_run_id="run-9",
        started_at=1.0, updated_at=5.0,
    )
    db.reconcile_session_index()
    item = db.list_session_index()["sessions"][0]
    # reconcile refreshes static fields but must NOT clobber live status
    assert item["running"] is True
    assert item["status"] == "running"
    assert item["active_run_id"] == "run-9"


def test_run_state_projects_into_session_index(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    # user-facing session has an index row (as session.create would create)
    db.upsert_session_index(session_id="s1", source="cli", started_at=1.0, updated_at=1.0)

    db.upsert_run(run_id="run-1", session_id="s1", status="running", runtime_session_id="rt-1")
    item = db.list_session_index()["sessions"][0]
    assert item["running"] is True
    assert item["status"] == "running"
    assert item["active_run_id"] == "run-1"
    assert item["active_runtime_session_id"] == "rt-1"

    db.upsert_run(run_id="run-1", session_id="s1", status="completed")
    item = db.list_session_index()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["active_run_id"] == ""


def test_run_state_does_not_create_index_row_for_member_sessions(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    # no session_index row exists (e.g. team-member runtime session)
    db.upsert_run(run_id="run-x", session_id="member-session", status="running")
    assert db.list_session_index(include_transient=True)["sessions"] == []


def test_run_terminal_does_not_clobber_other_active_run(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(session_id="s1", source="cli", started_at=1.0, updated_at=1.0)
    db.upsert_run(run_id="run-A", session_id="s1", status="running")
    # a different (stale) run terminating must not clear the active run-A
    db.upsert_run(run_id="run-B", session_id="s1", status="completed")
    item = db.list_session_index()["sessions"][0]
    assert item["running"] is True
    assert item["active_run_id"] == "run-A"
