from __future__ import annotations

import time
from pathlib import Path

from hermes_state import SessionDB


def _db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def _conversation(db: SessionDB, conversation_id: str = "conv-1") -> None:
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=f"{conversation_id}-session",
        title="Conversation",
        status="active",
    )


def _mission(
    db: SessionDB,
    mission_id: str,
    *,
    conversation_id: str = "conv-1",
    mission_status: str = "running",
    conversation_status: str = "active",
) -> None:
    db.upsert_team_mission(
        mission_id=mission_id,
        conversation_id=conversation_id,
        team_id="team-1",
        title=f"Mission {mission_id}",
        mode="supervised_mission",
        status=mission_status,
    )
    db.add_mission_to_conversation(
        conversation_id=conversation_id,
        mission_id=mission_id,
        status=conversation_status,
    )


def _bound_run(db: SessionDB, mission_id: str, run_id: str, *, status: str = "running") -> None:
    session_id = f"team:{mission_id}:node:worker"
    db.upsert_run(
        run_id=run_id,
        session_id=session_id,
        runtime_scope_key=session_id,
        status=status,
    )
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id="worker",
        run_id=run_id,
        session_id=session_id,
        runtime_scope_key=session_id,
    )


def test_reaper_skips_run_when_its_mission_active(tmp_path: Path):
    db = _db(tmp_path)
    _conversation(db)
    _mission(db, "mission-active", mission_status="running")
    _bound_run(db, "mission-active", "run-active")

    reaped = db.reap_terminal_mission_runs("mission-active")

    assert reaped == 0
    assert db.get_run("run-active")["status"] == "running"


def test_reaper_kills_run_when_mission_completed(tmp_path: Path):
    db = _db(tmp_path)
    _conversation(db)
    _mission(
        db,
        "mission-completed",
        mission_status="completed",
        conversation_status="completed",
    )
    _bound_run(db, "mission-completed", "run-orphan")

    reaped = db.reap_terminal_mission_runs("mission-completed")

    run = db.get_run("run-orphan")
    assert reaped == 1
    assert run["status"] in {"interrupted", "cancelled", "failed"}
    assert run["metadata"]["reaped_reason"] == "terminal_mission_stale_run"


def test_reaper_skips_running_run_when_other_mission_active_in_conv(tmp_path: Path):
    db = _db(tmp_path)
    _conversation(db)
    _mission(db, "mission-done", mission_status="completed", conversation_status="completed")
    _mission(db, "mission-active", mission_status="running", conversation_status="active")
    _bound_run(db, "mission-done", "run-latent-orphan")
    _bound_run(db, "mission-active", "run-live")

    reaped = db.reap_terminal_mission_runs("mission-done")

    assert reaped == 1
    assert db.get_run("run-latent-orphan")["status"] in {"interrupted", "cancelled", "failed"}
    assert db.get_run("run-live")["status"] == "running"


def test_reaper_handles_run_without_mission_id(tmp_path: Path):
    db = _db(tmp_path)
    _conversation(db)
    stale_at = time.time() - 1000
    db.upsert_run(
        run_id="run-legacy",
        session_id="conv-1-session",
        runtime_scope_key="conv-1-session",
        status="running",
        started_at=stale_at,
        updated_at=stale_at,
        metadata={"conversation_id": "conv-1"},
    )

    reaped = db.reap_terminal_mission_runs("mission-trigger")

    run = db.get_run("run-legacy")
    assert reaped == 1
    assert run["status"] in {"interrupted", "cancelled", "failed"}
    assert run["metadata"]["reaped_reason"].startswith("legacy_run_without_mission:")


def test_reaper_does_not_run_if_conv_has_active_mission(tmp_path: Path):
    db = _db(tmp_path)
    _conversation(db)
    _mission(db, "mission-active", mission_status="running", conversation_status="active")
    stale_at = time.time() - 1000
    db.upsert_run(
        run_id="run-legacy",
        session_id="conv-1-session",
        runtime_scope_key="conv-1-session",
        status="running",
        started_at=stale_at,
        updated_at=stale_at,
        metadata={"conversation_id": "conv-1"},
    )

    reaped = db.reap_terminal_mission_runs("mission-active")

    assert reaped == 0
    assert db.get_run("run-legacy")["status"] == "running"
