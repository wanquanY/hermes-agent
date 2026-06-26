from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hermes_state import SessionDB
from hermes_team_mission.runtime.failure import REASON_PROVIDER_RATE_LIMITED
from hermes_team_mission.runtime.scheduler import TeamMissionReadyScheduler


def _create_ready_mission(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="Worker A",
        status="ready",
    )
    return db


def test_scheduler_requeues_rate_limited_node_until_cooldown_expires(tmp_path: Path) -> None:
    db = _create_ready_mission(tmp_path)
    calls: list[dict[str, Any]] = []

    def start_node(_rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(params))
        if len(calls) == 1:
            return {
                "jsonrpc": "2.0",
                "id": _rid,
                "error": {"code": 429, "message": "provider rate limit exceeded"},
            }
        db.upsert_team_mission_node(
            mission_id=params["mission_id"],
            node_id=params["node_id"],
            kind="worker",
            title="Worker A",
            status="completed",
            metadata={"completed_by": "scheduler-test"},
        )
        return {
            "jsonrpc": "2.0",
            "id": _rid,
            "result": {"node": {"node_id": params["node_id"]}},
        }

    first = TeamMissionReadyScheduler(db=db, start_node=start_node).schedule_ready_nodes(
        mission_id="mission-1",
        trigger="test",
    )

    node = db.get_team_mission_node("mission-1", "node-a")
    assert [call["node_id"] for call in calls] == ["node-a"]
    assert first["errors"] == []
    assert first["skipped"][0]["reason"] == "provider_rate_limited"
    assert node["status"] == "ready"
    assert node["metadata"]["start_error_reason_code"] == REASON_PROVIDER_RATE_LIMITED
    assert node["metadata"]["start_error_recoverability"] == "retryable"
    assert node["metadata"]["rate_limit_cooldown_until"] > time.time()

    second = TeamMissionReadyScheduler(db=db, start_node=start_node).schedule_ready_nodes(
        mission_id="mission-1",
        trigger="test",
    )

    assert [call["node_id"] for call in calls] == ["node-a"]
    assert second["errors"] == []
    assert second["started"] == []
    assert second["skipped"][0]["reason"] == "provider_rate_limited_cooldown"
    assert db.get_team_mission_node("mission-1", "node-a")["status"] == "ready"

    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="Worker A",
        status="ready",
        metadata={"rate_limit_cooldown_until": time.time() - 1},
    )

    third = TeamMissionReadyScheduler(db=db, start_node=start_node).schedule_ready_nodes(
        mission_id="mission-1",
        trigger="test",
    )

    assert [call["node_id"] for call in calls] == ["node-a", "node-a"]
    assert third["errors"] == []
    assert third["started"][0]["node_id"] == "node-a"
    completed = db.get_team_mission_node("mission-1", "node-a")
    assert completed["status"] == "completed"
    assert completed["metadata"]["completed_by"] == "scheduler-test"
    assert completed["metadata"]["rate_limit_cooldown_until"] == 0


def test_scheduler_uses_provider_retry_after_for_rate_limit_cooldown(tmp_path: Path) -> None:
    db = _create_ready_mission(tmp_path)

    def start_node(_rid: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": _rid,
            "error": {
                "kind": "rate_limit",
                "message": "requests per minute exceeded",
                "retry_after_s": 7,
            },
        }

    before = time.time()
    TeamMissionReadyScheduler(db=db, start_node=start_node).schedule_ready_nodes(
        mission_id="mission-1",
        trigger="test",
    )

    metadata = db.get_team_mission_node("mission-1", "node-a")["metadata"]
    assert metadata["start_error_reason_code"] == REASON_PROVIDER_RATE_LIMITED
    assert metadata["rate_limit_cooldown_seconds"] == 7
    assert before + 6 <= metadata["rate_limit_cooldown_until"] <= before + 9


def test_scheduler_reclaims_stale_running_node_and_starts_it_again(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-a",
        kind="worker",
        title="Worker A",
        status="running",
        metadata={
            "heartbeat_at": time.time() - 10,
            "heartbeat_stale_after_seconds": 1,
        },
    )
    calls: list[str] = []

    def start_node(_rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["node_id"])
        return {"jsonrpc": "2.0", "id": _rid, "result": {"node": {"node_id": params["node_id"]}}}

    result = TeamMissionReadyScheduler(db=db, start_node=start_node).schedule_ready_nodes(
        mission_id="mission-1",
        trigger="test",
    )

    node = db.get_team_mission_node("mission-1", "node-a")
    assert calls == ["node-a"]
    assert result["started"][0]["node_id"] == "node-a"
    assert node["status"] == "starting"
    assert node["metadata"]["last_run_reason_code"] == "heartbeat_stale"
