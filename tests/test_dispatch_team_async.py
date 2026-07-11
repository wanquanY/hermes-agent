from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tools.dispatch_team_async as dispatch_tool
from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tools.registry import registry
from tui_gateway.methods.dispatch import dispatch_team_async
from tui_gateway.run_worker import DBRpcRequestFrame
from hermes_agent.orchestration.worker_supervisor import WorkerSupervisor


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _ids(*values: str):
    iterator = iter(values)
    return lambda: next(iterator)


def _params(**overrides: Any) -> dict[str, Any]:
    params = {
        "target_team_id": "team-1",
        "mission_objective": "Ship the release readiness report",
        "summary": "Release readiness",
        "files": ["docs/release.md"],
        "parent_conversation_id": "conv-parent",
    }
    params.update(overrides)
    return params


class _TeamMissionCreate:
    def __init__(self, db: CliSessionStore | None = None) -> None:
        self.db = db
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.pending_row: dict[str, Any] | None = None

    def __call__(self, rid, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((rid, params))
        if self.db is not None:
            self.pending_row = self.db.activities.get("act-1")
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "mission_id": params["mission_id"],
                "conversation_id": params["mission_id"],
                "leader_conversation_id": params["mission_id"],
                "graph": {"mission": {"mission_id": params["mission_id"]}},
            },
        }


@pytest.mark.asyncio
async def test_dispatch_team_creates_activity_with_pending_status(tmp_path: Path) -> None:
    db = _db(tmp_path)
    create = _TeamMissionCreate(db)

    await dispatch_team_async(
        _params(),
        db=db,
        team_mission_create=create,
        uuid_factory=_ids("act-1", "mission-1"),
        time_fn=lambda: 123.0,
    )

    assert create.pending_row is not None
    assert create.pending_row["activity_id"] == "act-1"
    assert create.pending_row["conversation_id"] == "conv-parent"
    assert create.pending_row["kind"] == "team_dispatch"
    assert create.pending_row["status"] == "pending"
    assert create.pending_row["target_team_id"] == "team-1"
    assert create.pending_row["target_mission_id"] is None
    assert create.pending_row["prompt_summary"] == "Release readiness"


@pytest.mark.asyncio
async def test_dispatch_team_returns_activity_id_and_mission_id(tmp_path: Path) -> None:
    db = _db(tmp_path)

    result = await dispatch_team_async(
        _params(),
        db=db,
        team_mission_create=_TeamMissionCreate(),
        uuid_factory=_ids("act-1", "mission-1"),
        time_fn=lambda: 123.0,
    )

    assert result == {"activity_id": "act-1", "mission_id": "mission-1"}
    assert db.activities.get("act-1")["status"] == "running"


@pytest.mark.asyncio
async def test_dispatch_team_links_target_mission_id_after_create(tmp_path: Path) -> None:
    db = _db(tmp_path)

    await dispatch_team_async(
        _params(),
        db=db,
        team_mission_create=_TeamMissionCreate(),
        uuid_factory=_ids("act-1", "mission-1"),
        time_fn=lambda: 123.0,
    )

    row = db.activities.get("act-1")
    assert row["status"] == "running"
    assert row["target_team_id"] == "team-1"
    assert row["target_mission_id"] == "mission-1"
    assert row["started_at"] == 123.0


@pytest.mark.asyncio
async def test_dispatch_team_missing_team_id_marks_activity_failed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    create = _TeamMissionCreate()

    result = await dispatch_team_async(
        _params(target_team_id=""),
        db=db,
        team_mission_create=create,
        uuid_factory=_ids("act-1", "mission-1"),
        time_fn=lambda: 123.0,
    )

    assert result == {
        "activity_id": "act-1",
        "mission_id": "mission-1",
        "status": "failed",
        "error": "target_team_id required",
    }
    assert create.calls == []
    row = db.activities.get("act-1")
    assert row["status"] == "failed"
    assert row["result_summary"] == "target_team_id required"
    assert row["completed_at"] == 123.0


@pytest.mark.asyncio
async def test_dispatch_team_with_parent_activity_id_links_correctly(tmp_path: Path) -> None:
    db = _db(tmp_path)

    await dispatch_team_async(
        _params(parent_activity_id="leader-activity"),
        db=db,
        team_mission_create=_TeamMissionCreate(),
        uuid_factory=_ids("act-1", "mission-1"),
    )

    row = db.activities.get("act-1")
    assert row["parent_activity_id"] == "leader-activity"
    assert row["conversation_id"] == "conv-parent"


@pytest.mark.asyncio
async def test_dispatch_team_invokes_existing_team_mission_create_flow(tmp_path: Path) -> None:
    db = _db(tmp_path)
    create = _TeamMissionCreate()

    await dispatch_team_async(
        _params(parent_activity_id="leader-activity"),
        db=db,
        team_mission_create=create,
        uuid_factory=_ids("act-1", "mission-1"),
    )

    assert len(create.calls) == 1
    _, create_params = create.calls[0]
    assert create_params["mission_id"] == "mission-1"
    assert create_params["team_id"] == "team-1"
    assert create_params["objective"] == "Ship the release readiness report"
    assert create_params["metadata"]["start_leader"] is True
    assert create_params["metadata"]["dispatch_activity_id"] == "act-1"
    assert create_params["metadata"]["parent_activity_id"] == "leader-activity"
    assert create_params["metadata"]["parent_conversation_id"] == "conv-parent"
    assert create_params["metadata"]["files"] == ["docs/release.md"]


@pytest.mark.asyncio
async def test_worker_dispatch_team_rpc_routes_through_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fake_dispatch(params):
        calls.append(params)
        return {"activity_id": "act-1", "mission_id": "mission-1"}

    monkeypatch.setattr("tui_gateway.methods.dispatch.dispatch_team_async", fake_dispatch)
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )

    reply = await supervisor._execute_worker_jsonrpc(
        DBRpcRequestFrame(
            id="worker-rpc-1",
            method="worker.dispatch_team_async",
            params={"target_team_id": "team-1"},
        )
    )

    assert reply.error is None
    assert reply.result == {"activity_id": "act-1", "mission_id": "mission-1"}
    assert calls == [{"target_team_id": "team-1"}]


def test_dispatch_team_tool_handler_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class _Proxy:
        def request(self, method: str, params: dict[str, Any]):
            calls.append((method, params))
            return {"activity_id": "act-1", "mission_id": "mission-1"}

    monkeypatch.setattr(dispatch_tool, "get_default_worker_rpc_proxy", lambda: _Proxy())
    parent = SimpleNamespace(
        session_id="conv-parent",
        run_context=SimpleNamespace(activity_id="leader-activity"),
    )

    payload = json.loads(
        registry.dispatch(
            "dispatch_team_async",
            {
                "team_id": "team-1",
                "mission_objective": "Do it together",
                "files": ["a.py"],
                "summary": "Do it",
            },
            parent_agent=parent,
        )
    )

    assert payload == {"activity_id": "act-1", "mission_id": "mission-1"}
    assert calls == [
        (
            "worker.dispatch_team_async",
            {
                "target_team_id": "team-1",
                "mission_objective": "Do it together",
                "files": ["a.py"],
                "summary": "Do it",
                "parent_conversation_id": "conv-parent",
                "parent_activity_id": "leader-activity",
            },
        )
    ]


def test_dispatch_team_tool_registered_in_registry() -> None:
    entry = registry.get_entry("dispatch_team_async")

    assert entry is not None
    assert entry.toolset == "subagent"
    schema = registry.get_schema("dispatch_team_async")
    assert schema["parameters"]["required"] == ["team_id", "mission_objective"]


async def _noop(*args, **kwargs) -> None:
    return None
