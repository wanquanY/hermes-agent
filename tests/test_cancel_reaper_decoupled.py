from __future__ import annotations

from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway


CONVERSATION_ID = "conversation-1"
CONVERSATION_SESSION_ID = "team-session-1"


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _wire_gateway_db(monkeypatch, db: CliSessionStore) -> None:
    team_mission = team_mission_gateway()
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)


def _create_conversation(db: CliSessionStore, *, active_mission_id: str = "") -> None:
    db.upsert_team_mission_conversation(
        conversation_id=CONVERSATION_ID,
        conversation_session_id=CONVERSATION_SESSION_ID,
        team_id="team-1",
        title="Team conversation",
        status="active",
        active_mission_id=active_mission_id,
    )


def _create_index(
    db: CliSessionStore,
    *,
    mission_id: str,
    active_run_id: str = "run-active",
) -> None:
    db.session_index.upsert(
        session_id=CONVERSATION_SESSION_ID,
        title="Team conversation",
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
        conversation_id=CONVERSATION_ID,
        team_id="team-1",
        mission_id=mission_id,
        running=True,
        status="running",
        active_run_id=active_run_id,
        active_execution_session_id=f"runtime-{active_run_id}",
        started_at=1.0,
        updated_at=2.0,
    )


def _create_mission(db: CliSessionStore, mission_id: str, *, status: str = "running") -> None:
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
        status="active" if status not in {"completed", "failed", "cancelled", "canceled"} else status,
    )
    db.activities.ensure_mission(
        conversation_id=CONVERSATION_SESSION_ID,
        mission_id=mission_id,
        status="running" if status not in {"completed", "failed", "cancelled", "canceled"} else "cancelled",
    )


def _bind_member_run(
    db: CliSessionStore,
    *,
    mission_id: str,
    run_id: str,
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
        execution_session_id=f"runtime-{run_id}",
        status=status,
    )
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=session_id,
        execution_session_id=f"runtime-{run_id}",
        runtime_scope_key=session_id,
        role="worker",
    )


def _cancel_via_gateway(monkeypatch, db: CliSessionStore, mission_id: str) -> tuple[dict, list[dict]]:
    from tui_gateway import server

    _wire_gateway_db(monkeypatch, db)
    canceled: list[dict] = []

    def fake_run_cancel(rid, params):
        canceled.append(dict(params))
        db.runs.upsert(
            run_id=params["run_id"],
            session_id=params["conversation_session_id"],
            execution_session_id=params["execution_session_id"],
            runtime_scope_key=params["runtime_scope_key"],
            status="cancelled",
        )
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "cancelled", **params}}

    monkeypatch.setitem(server._methods, "run.cancel", fake_run_cancel)
    response = server._methods["team_mission.cancel"](
        1,
        {"mission_id": mission_id, "canceled_by": "user", "reason": "stop"},
    )
    assert "error" not in response
    return response["result"], canceled


def test_team_mission_cancel_marks_only_mission_activity_cancelled(monkeypatch, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _create_conversation(db, active_mission_id="mission-A")
    _create_mission(db, "mission-A")
    _create_mission(db, "mission-B")

    _cancel_via_gateway(monkeypatch, db, "mission-A")

    activity_a = db.activities.get_for_mission("mission-A")
    activity_b = db.activities.get_for_mission("mission-B")
    assert activity_a is not None
    assert activity_b is not None
    assert activity_a["status"] == "cancelled"
    assert activity_a["completed_at"] is not None
    assert activity_b["status"] == "running"
    assert [row["target_mission_id"] for row in db.activities.list_active_missions(CONVERSATION_SESSION_ID)] == [
        "mission-B"
    ]


def test_team_mission_cancel_does_not_idle_conversation_with_other_active_runs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _create_conversation(db, active_mission_id="mission-A")
    _create_mission(db, "mission-A")
    _bind_member_run(db, mission_id="mission-A", run_id="run-mission-A")
    db.runs.upsert(run_id="run-chat", session_id=CONVERSATION_SESSION_ID, status="running")
    _create_index(db, mission_id="mission-A", active_run_id="run-chat")

    _cancel_via_gateway(monkeypatch, db, "mission-A")

    row = db.session_index.get(CONVERSATION_SESSION_ID)
    assert row is not None
    assert row["running"] is True
    assert row["status"] == "running"
    assert row["active_run_id"] == "run-chat"
    assert db.runs.get("run-mission-A")["status"] == "cancelled"
    assert db.runs.get("run-chat")["status"] == "running"


def test_team_mission_cancel_does_cancel_its_own_member_runs(monkeypatch, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _create_conversation(db, active_mission_id="mission-A")
    _create_mission(db, "mission-A")
    _create_mission(db, "mission-B")
    _bind_member_run(db, mission_id="mission-A", run_id="run-A")
    _bind_member_run(db, mission_id="mission-B", run_id="run-B")

    result, canceled = _cancel_via_gateway(monkeypatch, db, "mission-A")

    assert [item["run_id"] for item in result["canceled_runs"]] == ["run-A"]
    assert [item["run_id"] for item in canceled] == ["run-A"]
    assert db.runs.get("run-A")["status"] == "cancelled"
    assert db.runs.get("run-B")["status"] == "running"


def test_multi_mission_parallel_cancel_one_does_not_affect_others(monkeypatch, tmp_path: Path) -> None:
    db = _db(tmp_path)
    _create_conversation(db, active_mission_id="mission-B")
    _create_mission(db, "mission-A")
    _create_mission(db, "mission-B")
    _bind_member_run(db, mission_id="mission-A", run_id="run-A")
    _bind_member_run(db, mission_id="mission-B", run_id="run-B")
    _create_index(db, mission_id="mission-A", active_run_id="run-B")

    _cancel_via_gateway(monkeypatch, db, "mission-A")

    assert db.team_mission_graphs.get_team_mission_graph("mission-A")["mission"]["status"] == "cancelled"
    assert db.team_mission_graphs.get_team_mission_graph("mission-B")["mission"]["status"] == "running"
    assert db.activities.get_for_mission("mission-A")["status"] == "cancelled"
    assert db.activities.get_for_mission("mission-B")["status"] == "running"
    row = db.session_index.get(CONVERSATION_SESSION_ID)
    assert row is not None
    assert row["running"] is True
    assert row["active_run_id"] == "run-B"


def test_reaper_only_idles_conversation_when_all_runs_terminal_AND_no_active_missions(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _create_conversation(db, active_mission_id="mission-B")
    _create_mission(db, "mission-A", status="cancelled")
    _create_mission(db, "mission-B")
    db.runs.upsert(run_id="run-terminal", session_id=CONVERSATION_SESSION_ID, status="completed")
    _create_index(db, mission_id="mission-A", active_run_id="run-terminal")

    db.session_index.reconcile()

    row = db.session_index.get(CONVERSATION_SESSION_ID)
    assert row is not None
    assert row["running"] is True
    assert row["status"] == "running"

    db.set_conversation_mission_status(
        conversation_id=CONVERSATION_ID,
        mission_id="mission-B",
        status="cancelled",
    )
    db.session_index.reconcile()

    row = db.session_index.get(CONVERSATION_SESSION_ID)
    assert row is not None
    assert row["running"] is False
    assert row["status"] == "idle"
    assert row["active_run_id"] == ""
