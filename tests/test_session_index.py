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


def test_team_conversation_projects_into_session_index(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        stable_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    items = db.list_session_index()["sessions"]
    assert len(items) == 1
    it = items[0]
    assert it["session_id"] == "team-session-1"
    assert it["session_kind"] == "team_mission"
    assert it["team_id"] == "team-1"
    assert it["mission_id"] == "mission-1"
    assert it["title"] == "团队会话"


def test_update_session_index_for_mission_sets_running(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-1", team_id="team-1",
        stable_session_id="team-session-1", title="t", active_mission_id="mission-1",
    )
    assert db.update_session_index_for_mission("mission-1", status="running", running=True) == 1
    it = db.list_session_index()["sessions"][0]
    assert it["running"] is True and it["status"] == "running"

    db.update_session_index_for_mission("mission-1", status="waiting_approval", running=False, waiting_approval=True)
    it = db.list_session_index()["sessions"][0]
    assert it["waiting_approval"] is True and it["status"] == "waiting_approval"


def test_team_conversation_touch_preserves_live_mission_status(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-1", team_id="team-1",
        stable_session_id="team-session-1", title="t", active_mission_id="mission-1",
    )
    db.update_session_index_for_mission("mission-1", status="running", running=True)
    # a later conversation touch (e.g. title update) must NOT reset running
    db.upsert_team_mission_conversation(
        conversation_id="conv-1", team_id="team-1",
        stable_session_id="team-session-1", title="renamed", active_mission_id="mission-1",
        replace_title=True,
    )
    it = db.list_session_index()["sessions"][0]
    assert it["title"] == "renamed"
    assert it["running"] is True
    assert it["status"] == "running"


def test_reconcile_excludes_team_internal_node_sessions(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv-1", source="cli")
    db.create_session("team:mission-abc:node:verify-x", source="cli")
    db.reconcile_session_index()
    ids = _ids(db.list_session_index())
    assert "conv-1" in ids
    assert "team:mission-abc:node:verify-x" not in ids


def test_reconcile_purges_previously_leaked_node_rows(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    # simulate a leaked node-session row from an earlier reconcile
    db.upsert_session_index(session_id="team:mission-abc:node:synthesis", source="cli", started_at=1.0, updated_at=1.0)
    assert "team:mission-abc:node:synthesis" in _ids(db.list_session_index())
    db.reconcile_session_index()
    assert "team:mission-abc:node:synthesis" not in _ids(db.list_session_index())


def test_reconcile_excludes_team_node_delegate_task_children(tmp_path: Path):
    """delegate_task spawns a fresh tui session whose parent_session_id points at
    the team node session. Such children must not surface in the sidebar — every
    team task that runs delegate_task would otherwise leak worker chatter as
    unattributed `tui` rows ('Get latest GitHub stats' / empty '新会话')."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv-1", source="cli")
    db.create_session("team:mission-xyz:node:tech-research", source="cli")
    db.create_session(
        "20260621_195327_cc3473",
        source="tui",
        parent_session_id="team:mission-xyz:node:tech-research",
    )
    db.reconcile_session_index()
    ids = _ids(db.list_session_index())
    assert "conv-1" in ids
    assert "team:mission-xyz:node:tech-research" not in ids
    assert "20260621_195327_cc3473" not in ids


def test_reconcile_purges_previously_leaked_delegate_task_children(tmp_path: Path):
    """A prior reconcile that ran before the parent-filter fix could have inserted
    delegate_task children into the index. Reconcile must clean them up too."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("team:mission-xyz:node:tech-research", source="cli")
    db.create_session(
        "20260621_195327_cc3473",
        source="tui",
        parent_session_id="team:mission-xyz:node:tech-research",
    )
    # simulate the leak from before the fix
    db.upsert_session_index(
        session_id="20260621_195327_cc3473", source="tui", started_at=1.0, updated_at=1.0,
    )
    assert "20260621_195327_cc3473" in _ids(db.list_session_index())
    db.reconcile_session_index()
    assert "20260621_195327_cc3473" not in _ids(db.list_session_index())


def test_reconcile_heals_terminal_mission_conversation_stuck_running(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="m-x", team_id="t", title="T", mode="supervised_mission", status="cancelled")
    db.upsert_team_mission_conversation(
        conversation_id="c-x", team_id="t", stable_session_id="team-session-x",
        title="t", active_mission_id="m-x",
    )
    # simulate the stale running projection (cancel bypassed the reducer)
    db.update_session_index_for_mission("m-x", status="running", running=True)
    assert db.list_session_index()["sessions"][0]["running"] is True

    db.reconcile_session_index()

    item = db.list_session_index()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"


def test_cancel_team_mission_sets_conversation_index_idle(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="m-y", team_id="t", title="T", mode="supervised_mission", status="running")
    db.upsert_team_mission_conversation(
        conversation_id="c-y", team_id="t", stable_session_id="team-session-y",
        title="t", active_mission_id="m-y",
    )
    db.update_session_index_for_mission("m-y", status="running", running=True)
    assert db.list_session_index()["sessions"][0]["running"] is True

    db.cancel_team_mission(mission_id="m-y", canceled_by="user")

    item = db.list_session_index()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"


def test_update_for_mission_not_running_clears_active_run_id(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_session_index(
        session_id="team-session-z", session_kind="team_mission", mission_id="m-z",
        running=True, status="running", active_run_id="team-leader-run-z",
        active_runtime_session_id="rt-z", started_at=1.0, updated_at=1.0,
    )

    db.update_session_index_for_mission("m-z", status="idle", running=False)

    item = db.list_session_index()["sessions"][0]
    assert item["running"] is False
    # the sidebar treats a non-empty active_run_id as running — it MUST be cleared.
    assert item["active_run_id"] == ""
    assert item["active_runtime_session_id"] == ""


def test_reconcile_clears_stale_active_run_on_already_idle_terminal_mission(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="m-w", team_id="t", title="T", mode="supervised_mission", status="completed")
    db.upsert_team_mission_conversation(
        conversation_id="c-w", team_id="t", stable_session_id="team-session-w",
        title="t", active_mission_id="m-w",
    )
    # running/status already healed to idle, but a stale active_run_id lingers —
    # the exact state that kept finished team conversations spinning after restart.
    db.upsert_session_index(
        session_id="team-session-w", session_kind="team_mission", mission_id="m-w",
        running=False, status="idle", active_run_id="team-leader-run-w",
        active_runtime_session_id="rt-w", started_at=1.0, updated_at=1.0,
    )

    db.reconcile_session_index()

    item = db.list_session_index()["sessions"][0]
    assert item["active_run_id"] == ""
    assert item["active_runtime_session_id"] == ""
