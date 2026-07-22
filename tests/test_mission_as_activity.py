from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _workspace_payload(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(workspace)}


def _seed_team(db: CliSessionStore, tmp_path: Path) -> None:
    db.profiles.upsert_agent_profile(
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
    db.teams.upsert_agent_team(
        team_id="team-1",
        name="Team One",
        description="A test team.",
        lead_agent_profile_id="profile-leader",
    )
    db.teams.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning"],
        profile_name="Leader",
    )


def _create_mission_via_gateway(db: CliSessionStore, monkeypatch, tmp_path: Path, mission_id: str = "mission-1") -> dict:
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
                "conversation_session_id": params.get("conversation_session_id") or "",
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

    row = db.activities.create(
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

    activity = db.activities.get_for_mission("mission-1")
    assert activity is not None
    assert activity["activity_id"] == "mission:mission-1"
    assert activity["conversation_id"] == "team-session-1"
    assert activity["kind"] == "mission"
    assert activity["target_mission_id"] == "mission-1"
    assert activity["status"] == "running"


def test_team_mission_create_publishes_root_activity_to_visible_conversation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _seed_team(db, tmp_path)

    _create_mission_via_gateway(db, monkeypatch, tmp_path)

    upserts = [
        event
        for event in db.runs.list_events("team-session-1")
        if event.get("type") == "activity.upserted"
    ]
    assert upserts
    assert {
        event["payload"]["activity"]["activity_id"] for event in upserts
    } == {"mission:mission-1"}
    activity = upserts[-1]["payload"]["activity"]
    assert activity["activity_id"] == "mission:mission-1"
    assert activity["kind"] == "mission"
    assert activity["status"] == "pending"
    assert activity["title"] == "Ship the plan"


def test_team_mission_nodes_share_one_activity_projection_for_live_and_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from hermes_team_mission.gateway.conversation_owner_entities import (
        publish_team_mission_activity_entities,
    )
    from hermes_team_mission.read_models.conversation_activity_projection import (
        project_conversation_activities,
    )

    db = _db(tmp_path)
    _seed_team(db, tmp_path)
    _create_mission_via_gateway(db, monkeypatch, tmp_path)
    worker = db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="worker-1",
        kind="worker",
        title="Write the file",
        objective="Create result.txt.",
        status="ready",
        assignee_profile_id="profile-leader",
    )
    root = next(
        node
        for node in db.team_mission_graphs.get_team_mission_graph("mission-1")["nodes"]
        if node["kind"] == "root"
    )
    db.upsert_team_mission_edge(
        mission_id="mission-1",
        from_node_id=root["node_id"],
        to_node_id=worker["node_id"],
    )

    live = publish_team_mission_activity_entities(db, mission_id="mission-1")
    event_count = len(db.runs.list_events("team-session-1"))
    repeated = publish_team_mission_activity_entities(db, mission_id="mission-1")
    snapshot = project_conversation_activities(db, "team-session-1")

    expected_ids = {"mission:mission-1", "act-node:mission-1:worker-1"}
    assert {item["activity_id"] for item in live} == expected_ids
    assert repeated == live
    assert len(db.runs.list_events("team-session-1")) == event_count
    assert {item["activity_id"] for item in snapshot} == expected_ids
    worker_activity = next(
        item for item in live if item["activity_id"] == "act-node:mission-1:worker-1"
    )
    assert worker_activity["parent_activity_id"] == "mission:mission-1"
    assert worker_activity["dependency_activity_ids"] == ["mission:mission-1"]
    assert worker_activity["status"] == "pending"


def test_team_mission_activity_projection_keeps_owner_states_monotonic(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from hermes_team_mission.read_models.conversation_activity_projection import (
        project_team_mission_activities,
    )

    db = _db(tmp_path)
    _seed_team(db, tmp_path)
    _create_mission_via_gateway(db, monkeypatch, tmp_path)
    db.upsert_team_mission(
        mission_id="mission-1",
        status="ready",
        leader_session_id="team-session-1",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="worker-blocked",
        kind="worker",
        title="Wait for approval",
        status="blocked_waiting_dependency",
        assignee_profile_id="profile-leader",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="worker-future",
        kind="worker",
        title="Future state",
        status="future_owner_state",
        assignee_profile_id="profile-leader",
    )

    activities = {
        activity["activity_id"]: activity
        for activity in project_team_mission_activities(db, "mission-1")
    }

    assert activities["mission:mission-1"]["status"] == "running"
    assert activities["act-node:mission-1:worker-blocked"]["status"] == "pending"
    assert activities["act-node:mission-1:worker-future"]["status"] == "pending"


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
    activity = db.activities.get_for_mission("mission-1")
    assert activity is not None
    assert activity["status"] == "cancelled"
    assert activity["completed_at"] is not None
    assert db.activities.list_active_missions("team-session-1") == []


def test_team_mission_complete_marks_mission_activity_completed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
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
    activity = db.activities.get_for_mission("mission-1")
    assert activity is not None
    assert activity["status"] == "completed"
    assert activity["completed_at"] is not None


def test_list_active_mission_activities_excludes_terminal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.activities.ensure_mission(conversation_id="team-session-1", mission_id="mission-running")
    db.activities.ensure_mission(conversation_id="team-session-1", mission_id="mission-completed")
    completed = db.activities.get_for_mission("mission-completed")
    assert completed is not None
    db.activities.mark_completed(completed["activity_id"], result_summary="done", result_json={})

    rows = db.activities.list_active_missions("team-session-1")

    assert [row["target_mission_id"] for row in rows] == ["mission-running"]


def test_reconcile_mission_activities_one_shot_backfills_existing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
        team_id="team-1",
        title="Legacy active mission",
        active_mission_id="mission-legacy",
    )
    activity = db.activities.get_for_mission("mission-legacy")
    if activity is not None:
        db._conn.execute("DELETE FROM activities WHERE activity_id = ?", (activity["activity_id"],))  # noqa: SLF001
    db._conn.execute(  # noqa: SLF001
        "DELETE FROM state_meta WHERE key = 'mission_activities_backfill_cr_p3_1'"
    )

    result = db.activities.reconcile_missions_once()
    second = db.activities.reconcile_missions_once()

    assert result == {"ran": True, "inserted": 1}
    assert second == {"ran": False, "inserted": 0}
    activity = db.activities.get_for_mission("mission-legacy")
    assert activity is not None
    assert activity["conversation_id"] == "team-session-1"
    assert activity["status"] == "running"


def test_render_snapshot_includes_missions_top_level_list(monkeypatch, tmp_path: Path) -> None:
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    importlib.import_module("tui_gateway.methods.session_history")
    team_mission = team_mission_gateway()
    db = _db(tmp_path)
    db.sessions.create("team-session-1", source="team_mission")
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        conversation_session_id="team-session-1",
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
    db.activities.create(
        activity_id="act-member_chat:team-session-1:writer",
        conversation_id="team-session-1",
        kind="member_chat",
        target_profile_id="profile-writer",
        status="running",
        prompt_summary="Writer chat",
        notify_parent=False,
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
    assert {row["kind"] for row in response["result"]["activities"]} == {
        "member_chat",
        "mission",
    }


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

    db = open_cli_session_store(db_path)
    row = db.activities.create(
        activity_id="mission:legacy",
        conversation_id="team-session-1",
        kind="mission",
        target_mission_id="legacy",
        status="running",
    )

    assert row["kind"] == "mission"
