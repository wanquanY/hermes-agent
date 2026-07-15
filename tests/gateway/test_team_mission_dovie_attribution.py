from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from agent.dovie_attribution import build_dovie_attribution_headers
from channels.session_context import clear_session_vars, set_session_vars
from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server
from tui_gateway.run_worker import RunStartFrame, dovie_product_context_from_frame
from hermes_agent.orchestration import worker_runtime


CONVERSATION_ID = "conversation-1"
CONVERSATION_SESSION_ID = "team-session-1"
MISSION_ID = "mission-1"
TEAM_ID = "team-1"


def _workspace_payload(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(workspace)}


def _workspace_kwargs(tmp_path: Path) -> dict[str, str]:
    workspace = _workspace_payload(tmp_path)
    return {"workspace_id": workspace["workspace_id"], "workspace_path": workspace["workspace_path"]}


def _desktop_product_context() -> dict[str, Any]:
    return {
        "cloud_query": {
            "query_id": "query-team-1",
            "root_query_id": "root-query-1",
            "agent_run_id": "agent-run-root-1",
            "query_context_token": "query-token-1",
            "query_source": "team_conversation",
        },
        "team_mission": {
            "conversation_id": CONVERSATION_ID,
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "team_id": TEAM_ID,
        },
        "sourceAgentProfileId": "profile-leader",
        "sourceSessionId": CONVERSATION_SESSION_ID,
        "sourceRunId": "source-run-1",
        "sourceTurnId": "source-turn-1",
        "sourceClientMessageId": "client-message-1",
    }


def _leader_member(tmp_path: Path) -> dict[str, Any]:
    return {
        "member_id": "leader",
        "profile_id": "profile-leader",
        "profile_version_id": "version-leader",
        "role": "leader",
        "runtime_scope_key": "profile:profile-leader:version:version-leader",
        "dovie_profile": {
            "id": "profile-leader",
            "agentProfileVersionId": "version-leader",
            "runtimeScopeKey": "profile:profile-leader:version:version-leader",
            "hermesHomePath": str(tmp_path / "leader-home"),
        },
    }


def _worker_member(tmp_path: Path) -> dict[str, Any]:
    return {
        "member_id": "member-builder",
        "profile_id": "profile-builder",
        "profile_version_id": "version-builder",
        "role": "builder",
        "runtime_scope_key": "profile:profile-builder:version:version-builder",
        "dovie_profile": {
            "id": "profile-builder",
            "agentProfileVersionId": "version-builder",
            "runtimeScopeKey": "profile:profile-builder:version:version-builder",
            "hermesHomePath": str(tmp_path / "builder-home"),
        },
    }


class _FakeProcess:
    pid = 4242
    returncode = None


class _FakeWorker:
    process = _FakeProcess()
    active_runs: set[str] = set()

    def running(self) -> bool:
        return True


class _FakeLease:
    def __init__(self, scope_key: str, conversation_id: str) -> None:
        self.scope_key = scope_key
        self.worker_conversation_id = conversation_id
        self.worker = _FakeWorker()


class _FakeWorkerLeaseManager:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    async def get_or_spawn(self, conversation_id: str, _context: Any, *, scope_key: str) -> _FakeLease:
        return _FakeLease(scope_key, conversation_id)

    async def record_run_start(self, **kwargs: Any) -> None:
        self.starts.append(dict(kwargs))

    async def release(self, _conversation_id: str, *, scope_key: str) -> None:
        return None

    async def forget_run(self, _run_id: str) -> None:
        return None


class _FakeRouter:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.forgotten: list[str] = []

    def record_run_start(self, **kwargs: Any) -> None:
        self.starts.append(dict(kwargs))

    def forget_run(self, run_id: str) -> None:
        self.forgotten.append(run_id)


class _FakeSupervisor:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, RunStartFrame]] = []

    async def send(self, scope_key: str, conversation_id: str, frame: RunStartFrame) -> bool:
        self.sent.append((scope_key, conversation_id, frame))
        return True


class _RecordingTransport:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    async def write_async(self, frame: dict[str, Any]) -> bool:
        self.written.append(frame)
        return True


@pytest.fixture(autouse=True)
def _reset_worker_runtime():
    worker_runtime._reset_for_tests()
    yield
    worker_runtime._reset_for_tests()


@pytest.fixture
def worker_dispatch(monkeypatch: pytest.MonkeyPatch) -> _FakeSupervisor:
    fake_pool = _FakeWorkerLeaseManager()
    fake_router = _FakeRouter()
    fake_supervisor = _FakeSupervisor()
    monkeypatch.setattr(worker_runtime, "worker_pool", lambda: fake_pool)
    monkeypatch.setattr(worker_runtime, "worker_frame_router", lambda: fake_router)
    monkeypatch.setattr(worker_runtime, "worker_supervisor", lambda: fake_supervisor)
    return fake_supervisor


def _headers_from_frame(frame: RunStartFrame) -> dict[str, str]:
    raw_context = dovie_product_context_from_frame(frame)
    tokens = set_session_vars(dovie_product_context=raw_context)
    try:
        return build_dovie_attribution_headers()
    finally:
        clear_session_vars(tokens)


async def _dispatch_submit_params_to_frame(submit_params: dict[str, Any]) -> RunStartFrame:
    transport = _RecordingTransport()
    handled = await worker_runtime.primary_dispatch(
        {
            "jsonrpc": "2.0",
            "id": "rid-dispatch",
            "method": "run.submit",
            "params": dict(submit_params),
        },
        transport,
    )
    assert handled is True
    assert transport.written[0]["result"]["status"] == "queued"
    supervisor = worker_runtime.worker_supervisor()
    assert isinstance(supervisor, _FakeSupervisor)
    assert supervisor.sent
    return supervisor.sent[-1][2]


def _capture_team_submit_params(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_proxy_run_submit(params: dict[str, Any]) -> dict[str, bool]:
        captured.update(params)
        return {"ok": True}

    from hermes_team_mission.gateway import runtime_methods

    monkeypatch.setattr(runtime_methods, "_proxy_run_submit_via_worker", fake_proxy_run_submit)
    return captured


def _seed_mission(db: CliSessionStore, tmp_path: Path, *, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    members = [_leader_member(tmp_path), _worker_member(tmp_path)]
    db.initialize_team_mission_from_strategy(
        mission_id=MISSION_ID,
        conversation_id=CONVERSATION_ID,
        team_id=TEAM_ID,
        title="团队任务",
        objective="完成交付",
        **_workspace_kwargs(tmp_path),
        mode="supervised_mission",
        leader_session_id=CONVERSATION_SESSION_ID,
        metadata={"conversation_session_id": CONVERSATION_SESSION_ID, **(metadata or {})},
        members=members,
    )
    return members


@pytest.mark.asyncio
async def test_leader_message_submit_dovie_context_reaches_worker_frame(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_dispatch: _FakeSupervisor,
) -> None:
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", f"team:{CONVERSATION_ID}:leader-conversation")
    captured = _capture_team_submit_params(monkeypatch)

    response = server._methods["team_mission.message.submit"](
        "rid-leader",
        {
            "conversation_id": CONVERSATION_ID,
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "team_id": TEAM_ID,
            "text": "Leader 直接回复",
            "runtime_scope_key": f"team:{CONVERSATION_ID}:leader-conversation",
            "workspace": _workspace_payload(tmp_path),
            "dovie_product_context": _desktop_product_context(),
        },
    )

    assert "error" not in response, response
    frame = await _dispatch_submit_params_to_frame(captured)
    frame_context = json.loads(dovie_product_context_from_frame(frame))
    assert frame_context["cloud_query"] == _desktop_product_context()["cloud_query"]
    headers = _headers_from_frame(frame)
    assert headers["X-Dovie-Query-Id"] == "query-team-1"
    assert headers["X-Dovie-Query-Context-Token"] == "query-token-1"
    assert headers["X-Dovie-Agent-Role"] == "team_leader"


@pytest.mark.asyncio
async def test_member_chat_submit_dovie_context_reaches_worker_frame_with_member_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_dispatch: _FakeSupervisor,
) -> None:
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    members = _seed_mission(db, tmp_path)
    captured = _capture_team_submit_params(monkeypatch)

    response = server._methods["team_mission.message.submit"](
        "rid-member",
        {
            "mission_id": MISSION_ID,
            "conversation_id": CONVERSATION_ID,
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "team_id": TEAM_ID,
            "workspace": _workspace_payload(tmp_path),
            "text": "@Builder 帮我检查",
            "target_member_id": "member-builder",
            "members": members,
            "dovie_product_context": _desktop_product_context(),
        },
    )

    assert "error" not in response, response
    frame = await _dispatch_submit_params_to_frame(captured)
    frame_context = json.loads(dovie_product_context_from_frame(frame))
    assert frame_context["cloud_query"] == _desktop_product_context()["cloud_query"]
    assert frame_context["executing_agent_profile_id"] == "profile-builder"
    assert frame_context["agent_role"] == "team_member"
    headers = _headers_from_frame(frame)
    assert headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-builder"
    assert headers["X-Dovie-Agent-Role"] == "team_member"


@pytest.mark.asyncio
async def test_mission_message_submit_persists_context_for_node_start_fallback_and_worker_frame(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_dispatch: _FakeSupervisor,
) -> None:
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    members = _seed_mission(db, tmp_path)
    captured = _capture_team_submit_params(monkeypatch)

    response = server._methods["team_mission.message.submit"](
        "rid-mission-message",
        {
            "mission_id": MISSION_ID,
            "conversation_id": CONVERSATION_ID,
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "team_id": TEAM_ID,
            "workspace": _workspace_payload(tmp_path),
            "text": "启动这个任务",
            "members": members,
            "dovie_product_context": _desktop_product_context(),
        },
    )

    assert "error" not in response, response
    persisted = db.team_mission_graphs.get_team_mission_graph(MISSION_ID)["mission"]["metadata"]["dovie_product_context"]
    assert persisted["cloud_query"] == _desktop_product_context()["cloud_query"]

    db.upsert_team_mission_node(
        mission_id=MISSION_ID,
        node_id="node-worker",
        kind="worker",
        title="执行节点",
        objective="完成交付",
        status="ready",
        assignee_profile_id="profile-builder",
        assignee_profile_version_id="version-builder",
        output_contract={"format": "deliverable"},
    )
    captured.clear()
    response = server._methods["team_mission.node.start"](
        "rid-node",
        {
            "mission_id": MISSION_ID,
            "node_id": "node-worker",
            "run_id": "run-worker",
            "turn_id": "turn-worker",
        },
    )

    assert "error" not in response, response
    frame = await _dispatch_submit_params_to_frame(captured)
    frame_context = json.loads(dovie_product_context_from_frame(frame))
    assert frame_context["cloud_query"] == _desktop_product_context()["cloud_query"]
    assert frame_context["executing_agent_profile_id"] == "profile-builder"
    assert frame_context["agent_role"] == "team_member"
    assert _headers_from_frame(frame)["X-Dovie-Query-Id"] == "query-team-1"

    root_node = next(
        node
        for node in db.team_mission_graphs.get_team_mission_graph(MISSION_ID)["nodes"]
        if node.get("kind") == "root"
    )
    captured.clear()
    response = server._methods["team_mission.node.start"](
        "rid-root-node",
        {
            "mission_id": MISSION_ID,
            "node_id": root_node["node_id"],
            "run_id": "run-root",
            "turn_id": "turn-root",
            "use_strategy_prompt": True,
        },
    )

    assert "error" not in response, response
    frame = await _dispatch_submit_params_to_frame(captured)
    frame_context = json.loads(dovie_product_context_from_frame(frame))
    assert frame_context["cloud_query"] == _desktop_product_context()["cloud_query"]
    assert frame_context["executing_agent_profile_id"] == "profile-leader"
    assert frame_context["agent_role"] == "team_leader"


@pytest.mark.asyncio
async def test_worker_dispatch_warns_when_dovie_context_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    worker_dispatch: _FakeSupervisor,
) -> None:
    caplog.set_level(logging.WARNING, logger="hermes_agent.orchestration.worker_runtime")
    await _dispatch_submit_params_to_frame(
        {
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "run_id": "run-no-context",
            "turn_id": "turn-no-context",
            "runtime_scope_key": "profile:profile-leader",
            "agent_profile_id": "profile-leader",
            "text": "no attribution",
        }
    )

    messages = [record.getMessage() for record in caplog.records]
    assert any("dovie_attribution_context_missing" in message for message in messages)
