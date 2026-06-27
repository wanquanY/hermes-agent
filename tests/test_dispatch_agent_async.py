from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tools.dispatch_agent_async as dispatch_tool
from hermes_state import SessionDB
from tools.registry import registry
from tui_gateway.methods.dispatch import dispatch_agent_async
from tui_gateway.run_worker import DBRpcRequestFrame, RunStartFrame
from tui_gateway.services.worker_supervisor import WorkerSupervisor


def _db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def _profile(db: SessionDB, tmp_path: Path, profile_id: str = "profile-worker") -> dict[str, Any]:
    return db.upsert_agent_profile(
        profile_id=profile_id,
        slug=profile_id,
        name="Worker",
        hermes_home_path=str(tmp_path / profile_id),
        current_version_id="version-1",
    )


def _ids(*values: str):
    iterator = iter(values)
    return lambda: next(iterator)


def _params(**overrides: Any) -> dict[str, Any]:
    params = {
        "target_profile_id": "profile-worker",
        "prompt": "Investigate the failing shard",
        "summary": "Investigate shard",
        "files": ["tests/a.py"],
        "parent_conversation_id": "conv-parent",
    }
    params.update(overrides)
    return params


class _FakePool:
    def __init__(self, db: SessionDB | None = None) -> None:
        self.db = db
        self.spawn_calls: list[tuple[str, dict[str, Any]]] = []
        self.pending_row: dict[str, Any] | None = None
        self.recorded_runs: list[dict[str, Any]] = []
        self.released: list[str] = []
        self.forgotten: list[str] = []

    async def get_or_spawn(self, conversation_id: str, profile_context: dict[str, Any]):
        self.spawn_calls.append((conversation_id, profile_context))
        if self.db is not None:
            self.pending_row = self.db.get_activity("act-1")
        return SimpleNamespace(scope_key=conversation_id)

    async def record_run_start(self, **kwargs: Any) -> None:
        self.recorded_runs.append(dict(kwargs))

    async def release(self, conversation_id: str) -> None:
        self.released.append(conversation_id)

    async def forget_run(self, run_id: str) -> None:
        self.forgotten.append(run_id)


class _FailingPool(_FakePool):
    async def get_or_spawn(self, conversation_id: str, profile_context: dict[str, Any]):
        raise RuntimeError("spawn exploded")


class _FakeSupervisor:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, RunStartFrame]] = []

    async def send(self, scope_key: str, frame: RunStartFrame) -> bool:
        self.sent.append((scope_key, frame))
        return self.ok


class _FakeRouter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.forgotten: list[str] = []

    def record_run_start(self, **kwargs: Any) -> None:
        self.recorded.append(dict(kwargs))

    def forget_run(self, run_id: str) -> None:
        self.forgotten.append(run_id)


@pytest.mark.asyncio
async def test_dispatch_creates_activity_with_pending_status(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _profile(db, tmp_path)
    pool = _FakePool(db)

    await dispatch_agent_async(
        _params(),
        db=db,
        pool=pool,
        supervisor=_FakeSupervisor(),
        router=_FakeRouter(),
        uuid_factory=_ids("act-1", "conv-child", "run-1", "turn-1"),
        time_fn=lambda: 123.0,
    )

    assert pool.pending_row is not None
    assert pool.pending_row["activity_id"] == "act-1"
    assert pool.pending_row["conversation_id"] == "conv-parent"
    assert pool.pending_row["kind"] == "agent_dispatch"
    assert pool.pending_row["status"] == "pending"
    assert pool.pending_row["target_profile_id"] == "profile-worker"
    assert pool.pending_row["prompt_summary"] == "Investigate shard"


@pytest.mark.asyncio
async def test_dispatch_returns_activity_id_and_conversation_id(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _profile(db, tmp_path)

    result = await dispatch_agent_async(
        _params(),
        db=db,
        pool=_FakePool(),
        supervisor=_FakeSupervisor(),
        router=_FakeRouter(),
        uuid_factory=_ids("act-1", "conv-child", "run-1", "turn-1"),
        time_fn=lambda: 123.0,
    )

    assert result == {
        "activity_id": "act-1",
        "conversation_id": "conv-child",
        "status": "running",
    }
    assert db.get_activity("act-1")["status"] == "running"


@pytest.mark.asyncio
async def test_dispatch_spawns_worker_for_target_profile(tmp_path: Path) -> None:
    db = _db(tmp_path)
    profile = _profile(db, tmp_path)
    pool = _FakePool()

    await dispatch_agent_async(
        _params(),
        db=db,
        pool=pool,
        supervisor=_FakeSupervisor(),
        router=_FakeRouter(),
        uuid_factory=_ids("act-1", "conv-child", "run-1", "turn-1"),
    )

    assert pool.spawn_calls == [
        (
            "conv-child",
            {
                **profile,
                "agent_profile_id": "profile-worker",
                "agentProfileId": "profile-worker",
                "agent_profile_version_id": "version-1",
                "agentProfileVersionId": "version-1",
                "hermes_home": str(tmp_path / "profile-worker"),
                "hermesHomePath": str(tmp_path / "profile-worker"),
                "runtime_scope_key": "conv-child",
                "runtimeScopeKey": "conv-child",
            },
        )
    ]


@pytest.mark.asyncio
async def test_dispatch_pushes_run_start_frame_with_prompt(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _profile(db, tmp_path)
    supervisor = _FakeSupervisor()

    await dispatch_agent_async(
        _params(prompt="Do the long task"),
        db=db,
        pool=_FakePool(),
        supervisor=supervisor,
        router=_FakeRouter(),
        uuid_factory=_ids("act-1", "conv-child", "run-1", "turn-1"),
    )

    assert len(supervisor.sent) == 1
    scope_key, frame = supervisor.sent[0]
    assert scope_key == "conv-child"
    assert isinstance(frame, RunStartFrame)
    assert frame.run_id == "run-1"
    assert frame.turn_id == "turn-1"
    assert frame.stored_session_id == "conv-child"
    assert frame.prompt == "Do the long task"
    assert frame.params["dispatch_activity_id"] == "act-1"
    assert frame.params["files"] == ["tests/a.py"]
    assert frame.params["runtime_scope_key"] == "conv-child"


@pytest.mark.asyncio
async def test_dispatch_missing_target_profile_marks_activity_failed(tmp_path: Path) -> None:
    db = _db(tmp_path)

    result = await dispatch_agent_async(
        _params(),
        db=db,
        pool=_FakePool(),
        supervisor=_FakeSupervisor(),
        router=_FakeRouter(),
        uuid_factory=_ids("act-1", "conv-child", "run-1", "turn-1"),
        time_fn=lambda: 123.0,
    )

    assert result["activity_id"] == "act-1"
    assert result["conversation_id"] == "conv-child"
    assert result["status"] == "failed"
    row = db.get_activity("act-1")
    assert row["status"] == "failed"
    assert row["result_summary"] == "profile not found"
    assert row["completed_at"] == 123.0


@pytest.mark.asyncio
async def test_dispatch_worker_pool_spawn_failure_marks_activity_failed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _profile(db, tmp_path)

    result = await dispatch_agent_async(
        _params(),
        db=db,
        pool=_FailingPool(),
        supervisor=_FakeSupervisor(),
        router=_FakeRouter(),
        uuid_factory=_ids("act-1", "conv-child", "run-1", "turn-1"),
        time_fn=lambda: 123.0,
    )

    assert result == {
        "activity_id": "act-1",
        "conversation_id": "conv-child",
        "status": "failed",
        "error": "spawn exploded",
    }
    row = db.get_activity("act-1")
    assert row["status"] == "failed"
    assert row["result_summary"] == "spawn exploded"
    assert row["completed_at"] == 123.0


@pytest.mark.asyncio
async def test_dispatch_with_parent_activity_id_links_correctly(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _profile(db, tmp_path)

    await dispatch_agent_async(
        _params(parent_activity_id="leader-activity"),
        db=db,
        pool=_FakePool(),
        supervisor=_FakeSupervisor(),
        router=_FakeRouter(),
        uuid_factory=_ids("act-1", "conv-child", "run-1", "turn-1"),
    )

    row = db.get_activity("act-1")
    assert row["parent_activity_id"] == "leader-activity"
    assert row["conversation_id"] == "conv-parent"


@pytest.mark.asyncio
async def test_worker_dispatch_rpc_routes_through_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fake_dispatch(params):
        calls.append(params)
        return {"activity_id": "act-1", "conversation_id": "conv-child"}

    monkeypatch.setattr("tui_gateway.methods.dispatch.dispatch_agent_async", fake_dispatch)
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )

    reply = await supervisor._execute_worker_jsonrpc(
        DBRpcRequestFrame(
            id="worker-rpc-1",
            method="worker.dispatch_agent_async",
            params={"target_profile_id": "profile-worker"},
        )
    )

    assert reply.error is None
    assert reply.result == {"activity_id": "act-1", "conversation_id": "conv-child"}
    assert calls == [{"target_profile_id": "profile-worker"}]


def test_dispatch_tool_handler_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class _Proxy:
        def request(self, method: str, params: dict[str, Any]):
            calls.append((method, params))
            return {"activity_id": "act-1", "conversation_id": "conv-child"}

    monkeypatch.setattr(dispatch_tool, "get_default_worker_rpc_proxy", lambda: _Proxy())
    parent = SimpleNamespace(session_id="conv-parent")

    payload = json.loads(
        registry.dispatch(
            "dispatch_agent_async",
            {
                "target_profile_id": "profile-worker",
                "prompt": "Do it",
                "files": ["a.py"],
                "summary": "Do it",
            },
            parent_agent=parent,
        )
    )

    assert payload == {"activity_id": "act-1", "conversation_id": "conv-child"}
    assert calls == [
        (
            "worker.dispatch_agent_async",
            {
                "target_profile_id": "profile-worker",
                "prompt": "Do it",
                "files": ["a.py"],
                "summary": "Do it",
                "parent_conversation_id": "conv-parent",
            },
        )
    ]


def test_dispatch_tool_registered_in_registry() -> None:
    entry = registry.get_entry("dispatch_agent_async")

    assert entry is not None
    assert entry.toolset == "subagent"
    schema = registry.get_schema("dispatch_agent_async")
    assert schema["parameters"]["required"] == ["target_profile_id", "prompt"]


async def _noop(*args, **kwargs) -> None:
    return None
