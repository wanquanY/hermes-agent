from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_state import SessionDB
from hermes_team_mission.gateway import runtime_methods
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server
from tui_gateway.methods.dispatch import dispatch_agent_async
from tui_gateway.run_worker import ActivityEventFrame, EventFrame, RunTerminalFrame
from tui_gateway.services import run_control
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter


CONVERSATION_ID = "conversation-three-layer"
CONVERSATION_SESSION_ID = "team-session-three-layer"
TEAM_ID = "team-three-layer"
MISSION_ID = "mission-three-layer"
MEMBER_ID = "member-alpha"
MEMBER_PARTICIPANT_ID = f"member:{MEMBER_ID}"
LEADER_PARTICIPANT_ID = f"leader:{TEAM_ID}"


class _CaptureTransport:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, obj: dict[str, Any]) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        return None


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
        self.sent.append((scope_key, conversation_id, frame))
        return True


class _FakePool:
    def __init__(self) -> None:
        self.spawn_calls: list[tuple[str, dict[str, Any]]] = []
        self.recorded_runs: list[dict[str, Any]] = []
        self.released: list[str] = []

    async def get_or_spawn(self, conversation_id: str, profile_context: dict[str, Any]) -> Any:
        self.spawn_calls.append((conversation_id, dict(profile_context)))
        return SimpleNamespace(
            scope_key=profile_context["runtime_scope_key"],
            worker_conversation_id=conversation_id,
        )

    async def record_run_start(self, **kwargs: Any) -> None:
        self.recorded_runs.append(dict(kwargs))

    async def release(self, conversation_id: str) -> None:
        self.released.append(conversation_id)


class _FakeSupervisor:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
        self.sent.append((scope_key, conversation_id, frame))
        return True


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, db: SessionDB):
    conversation_render_snapshot = importlib.import_module(
        "tui_gateway.methods.conversation_render_snapshot"
    )
    session_history = importlib.import_module("tui_gateway.methods.session_history")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()

    monkeypatch.setattr(server, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(server, "_db_for_stable_session", lambda _stable: db, raising=False)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-test", raising=False)
    monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(session_history, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(session_methods, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(session_methods, "_SESSION_INDEX_RECONCILED", False, raising=False)
    monkeypatch.setattr(
        session_methods,
        "_bind_session_workspace",
        lambda **kwargs: kwargs.get("workspace") or {},
        raising=False,
    )
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setitem(
        server._methods,
        "run.submit",
        lambda rid, params: {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "run_id": params.get("run_id") or params.get("client_run_id") or "run-leader",
                "turn_id": params.get("turn_id") or "turn-leader",
                "conversation_session_id": params.get("conversation_session_id") or "",
                "runtime_scope_key": params.get("runtime_scope_key") or "",
                "status": "running",
            },
        },
    )
    return server, team_mission


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response
    return response["result"]


def _workspace(tmp_path: Path, name: str) -> dict[str, str]:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return {"workspace_id": name, "workspace_path": str(path)}


def _seed_profile(
    db: SessionDB,
    tmp_path: Path,
    profile_id: str,
    name: str,
) -> None:
    db.upsert_agent_profile(
        profile_id=profile_id,
        slug=profile_id,
        name=name,
        avatar=f"avatar://{profile_id}",
        description=f"{name} profile",
        category="engineering",
        tags=["test"],
        hermes_profile_name=profile_id,
        hermes_home_path=str(tmp_path / f"{profile_id}-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id=f"version-{profile_id}",
        current_version_number=1,
    )


def _seed_team(db: SessionDB, tmp_path: Path) -> None:
    _seed_profile(db, tmp_path, "profile-leader", "Leader")
    _seed_profile(db, tmp_path, "profile-alpha", "Alpha")
    _seed_profile(db, tmp_path, "profile-dispatch", "Dispatch Agent")
    db.upsert_agent_team(
        team_id=TEAM_ID,
        name="Three Layer Team",
        description="E2E team for final three-layer architecture validation.",
        lead_agent_profile_id="profile-leader",
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id=TEAM_ID,
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-profile-leader",
        role="lead",
        capability_tags=["planning"],
        profile_name="Leader",
        profile_avatar="avatar://profile-leader",
    )
    db.upsert_agent_team_member(
        member_id=MEMBER_ID,
        team_id=TEAM_ID,
        agent_profile_id="profile-alpha",
        agent_profile_version_id="version-profile-alpha",
        role="builder",
        capability_tags=["implementation"],
        profile_name="Alpha",
        profile_avatar="avatar://profile-alpha",
    )


def _create_direct_conversation(gateway_server: Any, tmp_path: Path) -> str:
    response = gateway_server.handle_request(
        {
            "id": "direct-create",
            "method": "session.create",
            "params": {
                "control_plane_only": True,
                "agent_profile_id": "profile-direct",
                "agent_profile_name": "Direct Agent",
                "runtime_scope_key": "profile:profile-direct",
                "title": "Direct three-layer conversation",
                "cwd": str(tmp_path),
                "workspace": {"id": "ws-direct", "path": str(tmp_path), "kind": "local"},
            },
        }
    )
    return str(_assert_ok(response)["conversation_session_id"])


def _create_team_conversation(
    team_mission: Any,
    tmp_path: Path,
    *,
    mission_id: str = MISSION_ID,
    conversation_id: str = CONVERSATION_ID,
    session_id: str = CONVERSATION_SESSION_ID,
) -> dict[str, str]:
    response = team_mission._methods["team_mission.create"](
        f"create-{mission_id}",
        {
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "conversation_session_id": session_id,
            "team_id": TEAM_ID,
            "title": f"Mission {mission_id}",
            "objective": "Validate complete three-layer invariants.",
            "mode": "supervised_mission",
            "workspace": _workspace(tmp_path, f"workspace-{mission_id}"),
            "metadata": {"start_leader": False},
            "record_user_task_message": False,
        },
    )
    _assert_ok(response)
    return {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "mission_id": mission_id,
        "team_id": TEAM_ID,
    }


def _message_complete(
    run_id: str,
    text: str,
    *,
    session_id: str = CONVERSATION_SESSION_ID,
    seq: int = 1,
    canonical_node_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text, "status": "complete"}
    if canonical_node_id:
        payload["canonical_node_id"] = canonical_node_id
        payload["canonicalNodeId"] = canonical_node_id
    return {
        "type": "message.complete",
        "session_id": session_id,
        "conversation_session_id": session_id,
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "seq": seq,
        "payload": payload,
    }


def _participants_by_id(db: SessionDB, session_id: str) -> dict[str, dict[str, Any]]:
    return {row["participant_id"]: row for row in db.list_conversation_participants(session_id)}


def _activities_by_kind(db: SessionDB, conversation_id: str) -> dict[str, dict[str, Any]]:
    return {row["kind"]: row for row in db.list_activities(conversation_id)}


def _published_payloads(transport: _CaptureTransport) -> list[dict[str, Any]]:
    return [
        frame["params"]
        for frame in transport.frames
        if frame.get("method") == "event" and isinstance(frame.get("params"), dict)
    ]


def _render(gateway_server: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _assert_ok(
        gateway_server._methods["conversation.render_snapshot"](
            "render",
            {"includeRunEvents": True, **params},
        )
    )


def _submit_member_chat(
    monkeypatch: pytest.MonkeyPatch,
    db: SessionDB,
    seeded_team: dict[str, str],
    *,
    run_id: str = "run-member-alpha",
) -> dict[str, Any]:
    captured_submit: dict[str, Any] = {}
    monkeypatch.setattr(
        runtime_methods,
        "_proxy_run_submit_via_worker",
        lambda params: captured_submit.update(params) or {"ok": True},
    )
    response = runtime_methods._submit_message_to_member(
        "member-submit",
        {
            "team_id": seeded_team["team_id"],
            "conversation_id": seeded_team["conversation_id"],
            "conversation_session_id": seeded_team["session_id"],
            "client_run_id": run_id,
            "turn_id": f"turn-{run_id}",
            "cwd": str(Path.cwd()),
            "workspace": {"workspace_id": "member-ws", "workspace_path": str(Path.cwd())},
        },
        db=db,
        target_member_id=MEMBER_ID,
        conversation_id=seeded_team["conversation_id"],
        conversation_session_id=seeded_team["session_id"],
        mission={
            "mission_id": seeded_team["mission_id"],
            "team_id": seeded_team["team_id"],
            "workspace_path": str(Path.cwd()),
        },
        text="@Alpha please handle the member chat.",
    )
    _assert_ok(response)
    assert captured_submit["runtime_scope_key"] == f"member-chat:{seeded_team['conversation_id']}:{MEMBER_ID}"
    return captured_submit


async def _record_member_reply(
    db: SessionDB,
    captured_submit: dict[str, Any],
    *,
    text: str = "Alpha completed the member reply.",
    canonical_node_id: str = "mission-three-layer:node-alpha",
) -> tuple[list[dict[str, Any]], _CaptureTransport]:
    transport = _CaptureTransport()
    run_control.subscribe_session(
        conversation_session_id=CONVERSATION_SESSION_ID,
        transport=transport,
        db=db,
    )
    router = WorkerFrameRouter(
        sender=_FakeSender(),
        publish_event=lambda params, **kwargs: run_control.publish_recorded_event(
            params,
            db=db,
            run_context=kwargs.get("run_context"),
        ),
        publish_run_terminal=lambda **kwargs: {},
    )
    router.record_run_start(
        scope_key=captured_submit["runtime_scope_key"],
        conversation_id=CONVERSATION_ID,
        run_id=captured_submit["run_id"],
        conversation_session_id=CONVERSATION_SESSION_ID,
        turn_id=captured_submit["turn_id"],
        run_context_json=captured_submit["run_context_json"],
    )
    await router.on_event(
        captured_submit["runtime_scope_key"],
        CONVERSATION_ID,
        EventFrame(
            params=_message_complete(
                captured_submit["run_id"],
                text,
                canonical_node_id=canonical_node_id,
            )
        ),
    )
    run_control.detach_transport(transport)
    return db.list_run_events(CONVERSATION_SESSION_ID, run_id=captured_submit["run_id"]), transport


def test_e2e_direct_conversation_three_layer_invariants(
    gateway,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    gateway_server, _team_mission = gateway

    session_id = _create_direct_conversation(gateway_server, tmp_path)

    row = db.get_session_index(session_id)
    participants = _participants_by_id(db, session_id)
    assert db.get_session(session_id) is not None
    assert row is not None
    assert row["conversation_kind"] == "direct"
    assert set(participants) == {"user", "agent:profile-direct"}
    assert participants["agent:profile-direct"]["role"] == "agent"
    assert db.list_activities(session_id) == []
    assert db.list_active_mission_activities(session_id) == []


def test_e2e_team_conversation_three_layer_invariants(
    gateway,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    _gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)

    team = _create_team_conversation(team_mission, tmp_path)

    row = db.get_session_index(team["session_id"])
    participants = _participants_by_id(db, team["session_id"])
    activity = db.get_activity_for_mission(team["mission_id"])
    assert row is not None
    assert row["conversation_kind"] == "team"
    assert row["team_id"] == TEAM_ID
    assert set(participants) == {
        "user",
        f"leader:{CONVERSATION_ID}",
        LEADER_PARTICIPANT_ID,
        MEMBER_PARTICIPANT_ID,
    }
    assert activity is not None
    assert activity["kind"] == "mission"
    assert activity["conversation_id"] == team["session_id"]
    assert activity["target_mission_id"] == team["mission_id"]
    assert activity["status"] == "running"


@pytest.mark.asyncio
async def test_e2e_member_chat_three_layer_invariants(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    _gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(team_mission, tmp_path)

    captured_submit = _submit_member_chat(monkeypatch, db, team)
    stored_events, transport = await _record_member_reply(db, captured_submit)
    [stored] = stored_events
    [envelope] = [
        payload
        for payload in _published_payloads(transport)
        if payload["type"] == "message.complete" and payload["run_id"] == captured_submit["run_id"]
    ]

    canonical_node_id = stored["payload"]["canonical_node_id"]
    assert stored["participant_id"] == MEMBER_PARTICIPANT_ID
    assert stored["payload"]["participant_id"] == MEMBER_PARTICIPANT_ID
    assert envelope["participant_id"] == MEMBER_PARTICIPANT_ID
    assert envelope["payload"]["participant_id"] == MEMBER_PARTICIPANT_ID
    assert canonical_node_id != MEMBER_PARTICIPANT_ID
    assert "leader" not in stored["participant_id"]


@pytest.mark.asyncio
async def test_e2e_async_agent_dispatch_three_layer(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    _gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(team_mission, tmp_path)
    sender = _FakeSender()
    router = WorkerFrameRouter(
        sender=sender,
        publish_event=lambda params, **kwargs: [],
        publish_run_terminal=lambda **kwargs: {},
    )

    result = await dispatch_agent_async(
        {
            "target_profile_id": "profile-dispatch",
            "prompt": "Run the async validation shard.",
            "summary": "Async validation shard",
            "parent_conversation_id": team["session_id"],
            "parent_scope_key": "leader-scope",
            "_parent_scope_key": "leader-scope",
        },
        db=db,
        pool=_FakePool(),
        supervisor=_FakeSupervisor(),
        router=router,
        uuid_factory=iter(["activity-dispatch", "conversation-child", "run-dispatch", "turn-dispatch"]).__next__,
        time_fn=lambda: 123.0,
    )
    monkeypatch.setattr(server, "_get_db", lambda: db, raising=False)
    await router.on_event(
        "profile:profile-dispatch",
        "conversation-child",
        EventFrame(params=_message_complete("run-dispatch", "Async worker finished.", session_id="conversation-child")),
    )
    await router.on_run_terminal(
        "profile:profile-dispatch",
        "conversation-child",
        RunTerminalFrame(run_id="run-dispatch", status="completed"),
    )

    activity = db.get_activity(result["activity_id"])
    assert result == {
        "activity_id": "activity-dispatch",
        "conversation_id": "conversation-child",
        "status": "running",
    }
    assert activity is not None
    assert activity["kind"] == "agent_dispatch"
    assert activity["conversation_id"] == team["session_id"]
    assert activity["status"] == "completed"
    assert sender.sent
    parent_scope, parent_conversation, frame = sender.sent[-1]
    assert parent_scope == "leader-scope"
    assert parent_conversation == team["session_id"]
    assert isinstance(frame, ActivityEventFrame)
    assert frame.event["activity_id"] == "activity-dispatch"
    assert frame.event["status"] == "completed"


@pytest.mark.asyncio
async def test_e2e_multi_activity_parallel_in_same_conversation(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    _gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(team_mission, tmp_path)

    captured_submit = _submit_member_chat(monkeypatch, db, team, run_id="run-member-parallel")
    db.create_activity(
        activity_id="activity-member-chat",
        conversation_id=team["session_id"],
        kind="member_chat",
        target_profile_id="profile-alpha",
        status="running",
        prompt_summary="Parallel member chat",
    )
    dispatch_result = await dispatch_agent_async(
        {
            "target_profile_id": "profile-dispatch",
            "prompt": "Run in parallel.",
            "summary": "Parallel dispatch",
            "parent_conversation_id": team["session_id"],
        },
        db=db,
        pool=_FakePool(),
        supervisor=_FakeSupervisor(),
        router=None,
        uuid_factory=iter(["activity-agent-dispatch", "conversation-child", "run-dispatch", "turn-dispatch"]).__next__,
    )

    activities = _activities_by_kind(db, team["session_id"])
    assert captured_submit["run_id"] == "run-member-parallel"
    assert dispatch_result["activity_id"] == "activity-agent-dispatch"
    assert activities["mission"]["status"] == "running"
    assert activities["member_chat"]["status"] == "running"
    assert activities["agent_dispatch"]["status"] == "running"
    assert activities["mission"]["activity_id"] == f"mission:{team['mission_id']}"
    assert activities["member_chat"]["activity_id"] != activities["agent_dispatch"]["activity_id"]


def test_e2e_mission_cancel_three_layer(
    gateway,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    _gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(team_mission, tmp_path)
    second = _create_team_conversation(
        team_mission,
        tmp_path,
        mission_id="mission-sibling",
        conversation_id=team["conversation_id"],
        session_id=team["session_id"],
    )
    db.create_activity(
        activity_id="activity-member-chat",
        conversation_id=team["session_id"],
        kind="member_chat",
        target_profile_id="profile-alpha",
        status="running",
    )
    db.create_activity(
        activity_id="activity-agent-dispatch",
        conversation_id=team["session_id"],
        kind="agent_dispatch",
        target_profile_id="profile-dispatch",
        status="running",
    )

    result = db.cancel_team_mission(
        mission_id=team["mission_id"],
        canceled_by="user",
        reason="final e2e cancel",
    )

    row = db.get_session_index(team["session_id"])
    activities = _activities_by_kind(db, team["session_id"])
    assert result["mission_status"] == "cancelled"
    assert row is not None
    assert row["conversation_kind"] == "team"
    assert db.get_session(team["session_id"]) is not None
    assert db.get_activity_for_mission(team["mission_id"])["status"] == "cancelled"
    assert db.get_activity_for_mission(second["mission_id"])["status"] == "running"
    assert activities["member_chat"]["status"] == "running"
    assert activities["agent_dispatch"]["status"] == "running"


def test_e2e_sidebar_single_source_session_index_only(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(team_mission, tmp_path)
    db.create_activity(
        activity_id="activity-sidebar-dispatch",
        conversation_id=team["conversation_id"],
        kind="agent_dispatch",
        target_profile_id="profile-dispatch",
        status="running",
        prompt_summary="sidebar active dispatch",
    )
    db.upsert_session_index(
        session_id=team["session_id"],
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
        team_id=team["team_id"],
        mission_id=team["mission_id"],
        conversation_id=team["conversation_id"],
        title="Three Layer Team",
        preview="sidebar row",
        message_count=1,
        started_at=1,
        updated_at=2,
    )
    monkeypatch.setitem(
        gateway_server._methods,
        "team_mission.conversation.list",
        lambda _rid, _params: pytest.fail("sidebar must not fetch team conversations"),
    )
    monkeypatch.setitem(
        gateway_server._methods,
        "team_mission.conversation.execution_session_ids",
        lambda _rid, _params: pytest.fail("sidebar must not fetch runtime session ids"),
    )

    result = _assert_ok(
        gateway_server._methods["session.index.list"](
            "sidebar",
            {"includeTransient": True, "conversationKind": "team"},
        )
    )

    [row] = result["sessions"]
    assert row["id"] == team["session_id"]
    assert row["conversation_kind"] == "team"
    assert row["team"]["id"] == TEAM_ID
    assert row["team"]["name"] == "Three Layer Team"
    assert row["team_name"] == "Three Layer Team"
    assert row["active_mission_id"] == team["mission_id"]
    assert row["active_activity_count"] == 1
    assert row["unread_completion_count"] == 0


@pytest.mark.asyncio
async def test_e2e_speaker_displays_member_name_not_leader_fallback(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
    db: SessionDB,
    tmp_path: Path,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(team_mission, tmp_path)
    captured_submit = _submit_member_chat(monkeypatch, db, team, run_id="run-member-speaker")
    await _record_member_reply(
        db,
        captured_submit,
        text="Alpha should render as the speaker.",
        canonical_node_id="mission-three-layer:leader-looking-node",
    )
    db.append_message(
        team["session_id"],
        role="assistant",
        content="Alpha should render as the speaker.",
        participant_id=MEMBER_PARTICIPANT_ID,
        metadata={
            "run_id": captured_submit["run_id"],
            "turn_id": captured_submit["turn_id"],
            "team_mission": {
                "participant_id": MEMBER_PARTICIPANT_ID,
                "display_name": "Leader",
                "canonical_node_id": "mission-three-layer:leader-looking-node",
            },
        },
    )

    snapshot = _render(gateway_server, {"session_id": team["session_id"]})
    message = next(item for item in snapshot["messages"] if item["text"] == "Alpha should render as the speaker.")
    participants = {item["participant_id"]: item for item in snapshot["participants"]}

    assert message["participant_id"] == MEMBER_PARTICIPANT_ID
    assert message["metadata"]["team_mission"]["display_name"] == "Leader"
    assert participants[message["participant_id"]]["display_name"] == "Alpha"
    assert participants[message["participant_id"]]["display_name"] != "Leader"
