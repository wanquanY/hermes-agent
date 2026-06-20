import importlib
from pathlib import Path

from hermes_state import SessionDB
from tui_gateway import server


def _setup(monkeypatch, tmp_path: Path) -> SessionDB:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    return db


def test_session_index_list_returns_mapped_items(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    db.upsert_session_index(
        session_id="s1",
        owner_agent_profile_id="agent-7",
        title="Hello",
        preview="hi",
        source="cli",
        running=True,
        status="running",
        active_run_id="run-1",
        started_at=10.0,
        updated_at=10.0,
    )
    resp = server._methods["session.index.list"](1, {})
    assert "error" not in resp
    sessions = resp["result"]["sessions"]
    assert len(sessions) == 1
    item = sessions[0]
    assert item["id"] == "s1"
    assert item["agentProfileId"] == "agent-7"
    assert item["running"] is True
    assert item["status"] == "running"
    assert item["active_run_id"] == "run-1"


def test_session_index_list_excludes_transient_by_default(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    db.upsert_session_index(session_id="real", started_at=1.0, updated_at=1.0)
    db.upsert_session_index(session_id="draft", transient=True, started_at=2.0, updated_at=2.0)

    resp = server._methods["session.index.list"](1, {})
    assert [s["id"] for s in resp["result"]["sessions"]] == ["real"]

    resp_all = server._methods["session.index.list"](1, {"include_transient": True})
    assert {s["id"] for s in resp_all["result"]["sessions"]} == {"real", "draft"}


def test_session_index_list_paginates_with_cursor(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    for i in range(5):
        db.upsert_session_index(session_id=f"s{i}", started_at=float(i), updated_at=float(i))

    first = server._methods["session.index.list"](1, {"limit": 2})
    assert first["result"]["pageInfo"]["hasMore"] is True
    cursor = first["result"]["pageInfo"]["nextCursor"]
    assert cursor

    second = server._methods["session.index.list"](1, {"limit": 2, "cursor": cursor})
    first_ids = [s["id"] for s in first["result"]["sessions"]]
    second_ids = [s["id"] for s in second["result"]["sessions"]]
    assert set(first_ids).isdisjoint(second_ids)
    assert first_ids == ["s4", "s3"]
    assert second_ids == ["s2", "s1"]


def test_project_session_index_on_create_persists_owner_profile(tmp_path: Path):
    from tui_gateway.methods import session as session_methods
    db = SessionDB(tmp_path / "state.db")
    session_methods._project_session_index_on_create(
        db, "s1", {"agentProfileId": "agent-7"}, "profile:agent-7", False
    )
    item = db.list_session_index()["sessions"][0]
    assert item["session_id"] == "s1"
    assert item["owner_agent_profile_id"] == "agent-7"
    assert item["runtime_scope_key"] == "profile:agent-7"


def test_project_session_index_derives_profile_from_scope(tmp_path: Path):
    from tui_gateway.methods import session as session_methods
    db = SessionDB(tmp_path / "state.db")
    session_methods._project_session_index_on_create(db, "s2", {}, "profile:agent-9", False)
    item = db.list_session_index()["sessions"][0]
    assert item["owner_agent_profile_id"] == "agent-9"


def test_project_session_index_marks_transient_draft(tmp_path: Path):
    from tui_gateway.methods import session as session_methods
    db = SessionDB(tmp_path / "state.db")
    session_methods._project_session_index_on_create(db, "draft", {"agentProfileId": "a"}, "profile:a", True)
    # transient drafts are excluded from the default sidebar read
    assert db.list_session_index()["sessions"] == []
    assert db.list_session_index(include_transient=True)["sessions"][0]["session_id"] == "draft"


def test_session_index_list_reconciles_preexisting_sessions_once(monkeypatch, tmp_path: Path):
    from tui_gateway.methods import session as session_methods
    db = _setup(monkeypatch, tmp_path)
    # session created before the index existed (no index row yet)
    db.create_session("legacy-1", source="cli")
    assert db.list_session_index()["sessions"] == []
    # first sidebar read backfills via reconcile
    monkeypatch.setattr(session_methods, "_SESSION_INDEX_RECONCILED", False)
    resp = server._methods["session.index.list"](1, {})
    ids = [s["id"] for s in resp["result"]["sessions"]]
    assert "legacy-1" in ids
