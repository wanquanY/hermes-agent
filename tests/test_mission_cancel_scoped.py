from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


CONVERSATION_ID = "conv-1"
CONVERSATION_SESSION_ID = "conv-1-session"


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _create_conversation(db: CliSessionStore) -> None:
    db.upsert_team_mission_conversation(
        conversation_id=CONVERSATION_ID,
        conversation_session_id=CONVERSATION_SESSION_ID,
        title="Conversation",
        status="active",
    )


def _mission(db: CliSessionStore, mission_id: str, *, status: str = "active") -> None:
    db.upsert_team_mission(
        mission_id=mission_id,
        conversation_id=CONVERSATION_ID,
        team_id="team-1",
        title=f"Mission {mission_id}",
        objective=f"Objective {mission_id}",
        mode="supervised_mission",
        status=status,
        leader_session_id=CONVERSATION_SESSION_ID,
    )
    db.add_mission_to_conversation(
        conversation_id=CONVERSATION_ID,
        mission_id=mission_id,
        status="active",
    )


def _bound_run(
    db: CliSessionStore,
    mission_id: str,
    run_id: str,
    *,
    status: str = "running",
) -> None:
    node_id = f"worker-{mission_id}"
    session_id = f"team:{mission_id}:node:{node_id}"
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind="worker",
        title=f"Worker {mission_id}",
        objective=f"Work {mission_id}",
        status="running",
        runtime_scope_key=session_id,
    )
    db.runs.upsert(
        run_id=run_id,
        session_id=session_id,
        runtime_scope_key=session_id,
        status=status,
    )
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=session_id,
        runtime_scope_key=session_id,
        role="worker",
    )


def _conversation_mission_statuses(db: CliSessionStore) -> dict[str, str]:
    return {
        row["mission_id"]: row["status"]
        for row in db.list_conversation_missions(CONVERSATION_ID)
    }


def test_cancel_mission_only_terminates_that_mission(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)
    _mission(db, "mission-A")
    _mission(db, "mission-B")

    result = db.cancel_team_mission(mission_id="mission-A", canceled_by="user")

    assert result["mission_status"] == "cancelled"
    assert db.team_mission_graphs.get_team_mission_graph("mission-A")["mission"]["status"] == "cancelled"
    assert db.team_mission_graphs.get_team_mission_graph("mission-B")["mission"]["status"] == "active"
    assert _conversation_mission_statuses(db) == {
        "mission-B": "active",
        "mission-A": "cancelled",
    }
    conversation = db.get_team_mission_conversation(CONVERSATION_ID)
    assert conversation["status"] == "active"
    assert conversation["active_mission_id"] == "mission-B"


def test_cancel_mission_terminates_its_runs(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)
    _mission(db, "mission-A")
    _bound_run(db, "mission-A", "run-x")

    result = db.cancel_team_mission(mission_id="mission-A", canceled_by="user")

    assert [binding["run_id"] for binding in result["cancel_run_bindings"]] == ["run-x"]
    assert db.runs.get("run-x")["status"] == "cancelled"


def test_cancel_mission_does_not_touch_sibling_runs(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)
    _mission(db, "mission-A")
    _mission(db, "mission-B")
    _bound_run(db, "mission-A", "run-x")
    _bound_run(db, "mission-B", "run-y")

    result = db.cancel_team_mission(mission_id="mission-A", canceled_by="user")

    assert [binding["run_id"] for binding in result["cancel_run_bindings"]] == ["run-x"]
    assert db.runs.get("run-x")["status"] == "cancelled"
    assert db.runs.get("run-y")["status"] == "running"
    assert db.team_mission_graphs.get_team_mission_graph("mission-B")["mission"]["status"] == "active"


def test_cancel_last_active_mission_does_not_cancel_conv(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)
    _mission(db, "mission-A")

    db.cancel_team_mission(mission_id="mission-A", canceled_by="user")

    conversation = db.get_team_mission_conversation(CONVERSATION_ID)
    assert conversation["status"] == "active"
    assert conversation["active_mission_id"] == ""
    assert db.has_active_mission(CONVERSATION_ID) is False
    assert _conversation_mission_statuses(db) == {"mission-A": "cancelled"}


def test_cancel_already_cancelled_mission_is_idempotent(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)
    _mission(db, "mission-A")

    first = db.cancel_team_mission(mission_id="mission-A", canceled_by="user")
    second = db.cancel_team_mission(mission_id="mission-A", canceled_by="user")

    assert first["mission_status"] == "cancelled"
    assert second["mission_status"] == "cancelled"
    assert db.team_mission_graphs.get_team_mission_graph("mission-A")["mission"]["status"] == "cancelled"
    assert db.get_team_mission_conversation(CONVERSATION_ID)["status"] == "active"
    assert _conversation_mission_statuses(db) == {"mission-A": "cancelled"}


def test_cancel_propagates_to_conversation_missions_join_status(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)
    _mission(db, "mission-A")
    _mission(db, "mission-B")

    db.cancel_team_mission(mission_id="mission-A", canceled_by="user")

    cancelled = db.list_conversation_missions(CONVERSATION_ID, status="cancelled")
    active = db.list_conversation_missions(CONVERSATION_ID, status="active")
    assert [row["mission_id"] for row in cancelled] == ["mission-A"]
    assert [row["mission_id"] for row in active] == ["mission-B"]
