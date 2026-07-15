import importlib
from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway import server


def _setup(monkeypatch, tmp_path: Path) -> CliSessionStore:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    return db


def test_session_index_list_returns_mapped_items(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    db.session_index.upsert(
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
    # Give the rows content so they survive the empty-draft suppression below;
    # this test is about transient exclusion, not the empty-row filter.
    db.session_index.upsert(session_id="real", title="Real", preview="hi", started_at=1.0, updated_at=1.0)
    db.session_index.upsert(session_id="draft", title="Draft", preview="hey", transient=True, started_at=2.0, updated_at=2.0)

    resp = server._methods["session.index.list"](1, {})
    assert [s["id"] for s in resp["result"]["sessions"]] == ["real"]

    resp_all = server._methods["session.index.list"](1, {"include_transient": True})
    assert {s["id"] for s in resp_all["result"]["sessions"]} == {"real", "draft"}


def test_session_index_list_paginates_with_cursor(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    for i in range(5):
        # Title so the rows aren't dropped by the empty-draft suppression.
        db.session_index.upsert(session_id=f"s{i}", title=f"S{i}", started_at=float(i), updated_at=float(i))

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


def _create_control_plane_session(
    monkeypatch,
    tmp_path: Path,
    **params,
) -> tuple[CliSessionStore, dict]:
    db = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-test")
    response = server._methods["session.create"](
        1,
        {
            "control_plane_only": True,
            "cwd": str(tmp_path),
            **params,
        },
    )
    assert "error" not in response
    return db, response["result"]


def test_session_create_persists_owner_profile(monkeypatch, tmp_path: Path):
    db, result = _create_control_plane_session(
        monkeypatch,
        tmp_path,
        agentProfileId="agent-7",
        agentProfileVersionId="version-7",
        runtime_scope_key="profile:agent-7",
    )

    item = db.session_index.get(result["conversation_session_id"])
    assert item["owner_agent_profile_id"] == "agent-7"
    assert item["owner_profile_version_id"] == "version-7"
    assert item["runtime_scope_key"] == "profile:agent-7"


def test_session_create_derives_profile_from_scope(monkeypatch, tmp_path: Path):
    db, result = _create_control_plane_session(
        monkeypatch,
        tmp_path,
        runtime_scope_key="profile:agent-9",
    )

    item = db.session_index.get(result["conversation_session_id"])
    assert item["owner_agent_profile_id"] == "agent-9"


def test_session_create_marks_transient_draft(monkeypatch, tmp_path: Path):
    db, result = _create_control_plane_session(
        monkeypatch,
        tmp_path,
        agentProfileId="a",
        runtime_scope_key="profile:a",
        transient=True,
    )

    # transient drafts are excluded from the default sidebar read
    assert db.session_index.list()["sessions"] == []
    [item] = db.session_index.list(include_transient=True)["sessions"]
    assert item["session_id"] == result["conversation_session_id"]


def test_session_index_list_hides_empty_draft_but_keeps_live_and_content(monkeypatch, tmp_path: Path):
    """Regression: control-plane session.create projects an empty session_index
    row the moment a new chat's composer opens (before the user types). Those
    empty-title / zero-message rows render as phantom "新会话" in the sidebar and
    re-appear on every launch. The sidebar read must hide them — but keep a brand
    -new chat that is mid-first-turn (running / has an active run) and any row
    that already has content."""
    db = _setup(monkeypatch, tmp_path)
    # Phantom empty draft (no title/preview/messages, not running) — must hide.
    db.session_index.upsert(session_id="empty-draft", source="tui", started_at=1.0, updated_at=1.0)
    # Empty but mid-first-turn (running) — must keep.
    db.session_index.upsert(
        session_id="live-new", source="tui", running=True, status="running",
        active_run_id="run-1", started_at=2.0, updated_at=2.0,
    )
    # Has content — must keep.
    db.session_index.upsert(
        session_id="real", title="Real chat", preview="hi", started_at=3.0, updated_at=3.0,
    )

    resp = server._methods["session.index.list"](1, {})
    ids = [s["id"] for s in resp["result"]["sessions"]]
    assert "empty-draft" not in ids
    assert "live-new" in ids
    assert "real" in ids


def test_session_index_list_emits_team_display_context_for_team_rows(monkeypatch, tmp_path: Path):
    """Conversation-architecture refactor (P2): the sidebar's single read now
    carries the team display (team name/avatar, lead profile name/avatar) and
    the conversation's objective — replacing the supplementary
    loadTeamConversationSidebarSessions stream and the merge heuristic."""
    db = _setup(monkeypatch, tmp_path)

    # Reference data the JOIN needs.
    db.teams.upsert_agent_team(
        team_id="team-1", name="Stellar", description="",
        lead_agent_profile_id="profile-leader",
    )
    db.profiles.upsert_agent_profile(
        profile_id="profile-leader", slug="leader", name="多多", avatar="🤖",
        hermes_home_path="/tmp/leader-home",
    )
    db.teams.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        role="lead",
    )
    db.ensure_team_mission_conversation(
        conversation_id="conv-1",
        conversation_session_id="team-session-1",
        team_id="team-1",
        title="星舰",
        objective="抵达火星",
    )
    # session_index for the team conversation (mirrors what upsert_team_mission_conversation does).
    db.session_index.upsert(
        session_id="team-session-1",
        title="星舰",
        preview="...",
        source="team_mission",
        session_kind="team_mission",
        team_id="team-1",
        conversation_id="conv-1",
        started_at=10.0,
        updated_at=10.0,
    )
    # Plain chat row — must NOT receive any team_ fields.
    db.session_index.upsert(
        session_id="plain-1", title="Plain", preview="hi",
        started_at=20.0, updated_at=20.0,
    )

    resp = server._methods["session.index.list"](1, {})
    by_id = {s["id"]: s for s in resp["result"]["sessions"]}

    team_item = by_id["team-session-1"]
    assert team_item["team_id"] == "team-1"
    assert team_item["team_name"] == "Stellar"
    assert team_item["team"]["name"] == "Stellar"
    assert team_item["team"]["lead_agent_profile_id"] == "profile-leader"
    assert team_item["team"]["leaderMember"]["agentProfileId"] == "profile-leader"
    assert team_item["team"]["leaderMember"]["profileAvatar"] == "🤖"
    assert team_item["team"]["displayMembers"][0]["agentProfileId"] == "profile-leader"
    assert team_item["team"]["displayMembers"][0]["profileAvatar"] == "🤖"
    assert team_item["lead_profile_name"] == "多多"
    assert team_item["lead_profile_avatar"] == "🤖"
    assert team_item["objective"] == "抵达火星"

    plain_item = by_id["plain-1"]
    assert "team" not in plain_item
    assert "team_name" not in plain_item
    assert "objective" not in plain_item


def test_session_index_startup_reconciles_preexisting_sessions_before_reads(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    # session created before the index existed (no index row yet). Give it a
    # message so reconcile backfills a non-empty row that survives the
    # empty-draft suppression on the sidebar read.
    db.sessions.create("legacy-1", source="cli")
    db.messages.append(session_id="legacy-1", role="user", content="hello")
    db.session_index.delete("legacy-1")
    assert db.session_index.list()["sessions"] == []
    db.close()

    db = open_cli_session_store(tmp_path / "state.db")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    resp = server._methods["session.index.list"](1, {})
    ids = [s["id"] for s in resp["result"]["sessions"]]
    assert "legacy-1" in ids
