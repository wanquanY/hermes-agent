from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from hermes_state import SessionDB
from tests.team_mission_gateway_test_support import team_mission_gateway


def _db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def _workspace_payload(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(workspace)}


def _seed_team(db: SessionDB, tmp_path: Path) -> None:
    db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader",
        description="Plans the mission.",
        category="product",
        tags=["planning"],
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "leader-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id="version-leader",
        current_version_number=1,
    )
    db.upsert_agent_team(
        team_id="team-1",
        name="Team One",
        description="A test team.",
        lead_agent_profile_id="profile-leader",
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning"],
        profile_name="Leader",
    )


def _create_mission_via_gateway(db: SessionDB, monkeypatch, tmp_path: Path, mission_id: str = "mission-1") -> dict:
    from tui_gateway import server

    team_mission = team_mission_gateway()
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setitem(
        server._methods,
        "run.submit",
        lambda _rid, params: {
            "jsonrpc": "2.0",
            "id": _rid,
            "result": {
                "run_id": params.get("run_id") or "run-leader",
                "stored_session_id": params.get("stored_session_id") or "",
                "runtime_scope_key": params.get("runtime_scope_key") or "",
                "status": "running",
            },
        },
    )
    response = server._methods["team_mission.create"](
        1,
        {
            "mission_id": mission_id,
            "conversation_id": "conversation-1",
            "conversation_session_id": "team-session-1",
            "team_id": "team-1",
            "title": "Ship the plan",
            "objective": "Create a plan.",
            "workspace": _workspace_payload(tmp_path),
            "record_user_task_message": False,
        },
    )
    assert "error" not in response
    return response["result"]


def test_activities_table_accepts_kind_mission(tmp_path: Path) -> None:
    db = _db(tmp_path)

    row = db.create_activity(
        activity_id="mission:mission-1",
        conversation_id="team-session-1",
        kind="mission",
        target_mission_id="mission-1",
        status="running",
    )

    assert row["kind"] == "mission"
    assert row["status"] == "running"
    assert row["target_mission_id"] == "mission-1"


def test_team_mission_create_inserts_mission_activity_row(monkeypatch, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_team(db, tmp_path)

    _create_mission_via_gateway(db, monkeypatch, tmp_path)

    activity = db.get_activity_for_mission("mission-1")
    assert activity is not None
    assert activity["activity_id"] == "mission:mission-1"
    assert activity["conversation_id"] == "team-session-1"
    assert activity["kind"] == "mission"
    assert activity["target_mission_id"] == "mission-1"
    assert activity["status"] == "running"


def test_team_mission_cancel_marks_mission_activity_cancelled(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server

    db = _db(tmp_path)
    _seed_team(db, tmp_path)
    _create_mission_via_gateway(db, monkeypatch, tmp_path)

    response = server._methods["team_mission.cancel"](
        2,
        {"mission_id": "mission-1", "reason": "stop"},
    )

    assert "error" not in response
    activity = db.get_activity_for_mission("mission-1")
    assert activity is not None
    assert activity["status"] == "cancelled"
    assert activity["completed_at"] is not None
    assert db.list_active_mission_activities("team-session-1") == []


def test_team_mission_complete_marks_mission_activity_completed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team",
        active_mission_id="mission-1",
    )

    result = db.set_conversation_mission_status(
        conversation_id="conversation-1",
        mission_id="mission-1",
        status="completed",
    )

    assert result is not None
    activity = db.get_activity_for_mission("mission-1")
    assert activity is not None
    assert activity["status"] == "completed"
    assert activity["completed_at"] is not None


def test_list_active_mission_activities_excludes_terminal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.ensure_mission_activity(conversation_id="team-session-1", mission_id="mission-running")
    db.ensure_mission_activity(conversation_id="team-session-1", mission_id="mission-completed")
    completed = db.get_activity_for_mission("mission-completed")
    assert completed is not None
    db.mark_activity_completed(completed["activity_id"], result_summary="done", result_json={})

    rows = db.list_active_mission_activities("team-session-1")

    assert [row["target_mission_id"] for row in rows] == ["mission-running"]


def test_reconcile_mission_activities_one_shot_backfills_existing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Legacy active mission",
        active_mission_id="mission-legacy",
    )
    activity = db.get_activity_for_mission("mission-legacy")
    if activity is not None:
        db._conn.execute("DELETE FROM activities WHERE activity_id = ?", (activity["activity_id"],))  # noqa: SLF001
    db._conn.execute(  # noqa: SLF001
        "DELETE FROM state_meta WHERE key = 'mission_activities_backfill_cr_p3_1'"
    )

    result = db.reconcile_mission_activities_one_shot()
    second = db.reconcile_mission_activities_one_shot()

    assert result == {"ran": True, "inserted": 1}
    assert second == {"ran": False, "inserted": 0}
    activity = db.get_activity_for_mission("mission-legacy")
    assert activity is not None
    assert activity["conversation_id"] == "team-session-1"
    assert activity["status"] == "running"


def test_render_snapshot_includes_missions_top_level_list(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    importlib.import_module("tui_gateway.methods.session_history")
    team_mission = team_mission_gateway()
    db = _db(tmp_path)
    db.create_session("team-session-1", source="team_mission")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        stable_session_id="team-session-1",
        team_id="team-1",
        title="Team",
        active_mission_id="mission-1",
    )
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Team",
        objective="Complete it.",
        mode="supervised_mission",
        status="running",
        leader_session_id="team-session-1",
    )
    monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)

    response = server._methods["conversation.render_snapshot"](
        3,
        {"conversation_id": "conversation-1"},
    )

    assert "error" not in response
    assert response["result"]["mission"]["mission_id"] == "mission-1"
    missions = response["result"]["missions"]
    assert [row["target_mission_id"] for row in missions] == ["mission-1"]
    assert missions[0]["kind"] == "mission"


def test_existing_activities_table_migrates_kind_mission_check(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE activities (
                activity_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                parent_activity_id TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('chat', 'agent_dispatch', 'team_dispatch', 'member_chat')),
                target_profile_id TEXT,
                target_team_id TEXT,
                target_mission_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
                prompt_summary TEXT,
                result_summary TEXT,
                result_json TEXT,
                started_at REAL,
                completed_at REAL,
                notify_parent INTEGER NOT NULL DEFAULT 1,
                read_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = SessionDB(db_path)
    row = db.create_activity(
        activity_id="mission:legacy",
        conversation_id="team-session-1",
        kind="mission",
        target_mission_id="legacy",
        status="running",
    )

    assert row["kind"] == "mission"
