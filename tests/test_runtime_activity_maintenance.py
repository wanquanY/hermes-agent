from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server

activity_methods = importlib.import_module("tui_gateway.methods.activity")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[CliSessionStore]:
    state = open_cli_session_store(tmp_path / "state.db")
    try:
        yield state
    finally:
        state.close()


@pytest.fixture(autouse=True)
def gateway_state(db: CliSessionStore) -> Iterator[None]:
    previous_db = server._db
    previous_db_error = server._db_error
    previous_db_by_home = dict(server._db_by_home)
    previous_db_error_by_home = dict(server._db_error_by_home)
    server._db = db
    server._db_error = None
    server._db_by_home = {}
    server._db_error_by_home = {}
    try:
        yield
    finally:
        server._db = previous_db
        server._db_error = previous_db_error
        server._db_by_home = previous_db_by_home
        server._db_error_by_home = previous_db_error_by_home


def _call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": f"test-{method}-{time.time_ns()}",
            "method": method,
            "params": params or {},
        }
    )
    assert isinstance(response, dict)
    return response


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return response["result"]


def _seed_mission(
    db: CliSessionStore,
    *,
    mission_id: str = "mission-1",
    status: str = "completed",
    run_id: str = "run-1",
    node_id: str = "node-1",
) -> None:
    db.upsert_team_mission(
        mission_id=mission_id,
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status=status,
    )
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind="worker",
        title="Node",
        status="done" if status != "running" else "running",
    )
    db.runs.upsert(
        run_id=run_id,
        session_id=f"team:{mission_id}:node:{node_id}",
        runtime_scope_key=f"team:{mission_id}:node:{node_id}",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=f"team:{mission_id}:node:{node_id}",
        runtime_scope_key=f"team:{mission_id}:node:{node_id}",
    )


def _append_prunable_delta(
    db: CliSessionStore,
    *,
    mission_id: str = "mission-1",
    run_id: str = "run-1",
) -> None:
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=run_id,
        event={
            "type": "message.delta",
            "seq": 1,
            "payload": {"delta": "token", "mode": "append"},
        },
    )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=run_id,
        event={"type": "message.complete", "seq": 2, "payload": {"text": "done"}},
    )


def test_maintenance_rejects_missing_activity_id() -> None:
    response = _call("runtime.activity.maintenance", {})

    assert response["error"]["code"] == 4006


def test_maintenance_rejects_malformed_activity_id() -> None:
    response = _call("runtime.activity.maintenance", {"activity_id": "not-valid"})

    assert response["error"]["code"] == 4006


def test_maintenance_returns_5008_when_db_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity_methods._server, "_get_db", lambda: None)

    response = _call(
        "runtime.activity.maintenance",
        {"activity_id": "mission:missing-db"},
    )

    assert response["error"]["code"] == 5008


def test_maintenance_mission_prefix_reaps_terminal_mission_active_runs(
    db: CliSessionStore,
) -> None:
    _seed_mission(db, status="completed")

    result = _assert_ok(
        _call("runtime.activity.maintenance", {"activity_id": "mission:mission-1"})
    )

    assert "reaped" in result["actions"]
    assert db.runs.get("run-1")["status"] in {"interrupted", "cancelled", "canceled"}


def test_maintenance_mission_prefix_prunes_terminal_mission_events(
    db: CliSessionStore,
) -> None:
    _seed_mission(db, status="running")
    _append_prunable_delta(db)
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="completed",
    )

    result = _assert_ok(
        _call("runtime.activity.maintenance", {"activity_id": "mission:mission-1"})
    )

    assert "pruned" in result["actions"]
    after_types = [
        event.get("payload", {}).get("source_event_type")
        for event in db.list_team_mission_events("mission-1")
    ]
    assert "message.delta" not in after_types
    assert "message.complete" in after_types


def test_maintenance_mission_prefix_no_op_when_mission_running(
    db: CliSessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_mission(db, status="running")
    calls: list[str] = []
    monkeypatch.setattr(
        db,
        "reap_terminal_mission_runs",
        lambda mission_id: calls.append(f"reap:{mission_id}") or 0,
    )
    monkeypatch.setattr(
        db,
        "prune_team_mission_events",
        lambda mission_id: calls.append(f"prune:{mission_id}") or 0,
    )

    result = _assert_ok(
        _call("runtime.activity.maintenance", {"activity_id": "mission:mission-1"})
    )

    assert result == {"activity_id": "mission:mission-1", "actions": [], "errors": []}
    assert calls == []


def test_maintenance_mission_prefix_returns_actions_list_with_reaped_and_pruned(
    db: CliSessionStore,
) -> None:
    _seed_mission(db, status="completed")

    result = _assert_ok(
        _call("runtime.activity.maintenance", {"activity_id": "mission:mission-1"})
    )

    assert result["actions"] == ["reaped", "pruned"]
    assert result["errors"] == []


def test_maintenance_mission_prefix_swallows_reaper_exception_into_errors_list(
    db: CliSessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_mission(db, status="completed")

    def fail_reaper(mission_id: str) -> int:
        raise RuntimeError(f"boom:{mission_id}")

    monkeypatch.setattr(db, "reap_terminal_mission_runs", fail_reaper)
    monkeypatch.setattr(db, "prune_team_mission_events", lambda mission_id: 0)

    result = _assert_ok(
        _call("runtime.activity.maintenance", {"activity_id": "mission:mission-1"})
    )

    assert result["actions"] == ["pruned"]
    assert len(result["errors"]) == 1
    assert "reap_terminal_mission_runs" in result["errors"][0]
    assert "boom:mission-1" in result["errors"][0]


def test_maintenance_team_conversation_prefix_is_noop_returns_empty_actions() -> None:
    result = _assert_ok(
        _call(
            "runtime.activity.maintenance",
            {"activity_id": "team-conversation:conversation-1"},
        )
    )

    assert result == {
        "activity_id": "team-conversation:conversation-1",
        "actions": [],
        "errors": [],
    }


def test_maintenance_chat_prefix_is_noop() -> None:
    result = _assert_ok(
        _call("runtime.activity.maintenance", {"activity_id": "chat:session-1"})
    )

    assert result == {"activity_id": "chat:session-1", "actions": [], "errors": []}


def test_maintenance_act_prefix_is_noop() -> None:
    result = _assert_ok(
        _call(
            "runtime.activity.maintenance",
            {"activity_id": "act-member_chat:conv-1:a"},
        )
    )

    assert result == {
        "activity_id": "act-member_chat:conv-1:a",
        "actions": [],
        "errors": [],
    }
