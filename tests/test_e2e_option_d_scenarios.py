from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from agent.activity_event_bus import ActivityEventBus
from agent.conversation_loop import _drain_activity_events_for_api
from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_agent.domain.participant_transcript_projector import project_participant_transcript
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway.run_worker import ActivityEventFrame, EventFrame, RunStartFrame, RunTerminalFrame
from tui_gateway.services.runtime_scope import RuntimeScope
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter
from hermes_agent.orchestration.worker_lease_manager import WorkerLeaseManager
from hermes_agent.orchestration.worker_supervisor import RunWorker


class _FakeProcess:
    _next_pid = 3000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = None

    async def wait(self) -> int | None:
        return self.returncode


class _FakeSupervisor:
    def __init__(self, parent_bus: ActivityEventBus | None = None) -> None:
        self.ensure_calls: list[RuntimeScope] = []
        self.shutdown_calls: list[tuple[str, str]] = []
        self.shutdown_all_called = False
        self.workers: dict[tuple[str, str], RunWorker] = {}
        self.sent_run_starts: list[tuple[str, str, RunStartFrame]] = []
        self.sent_activity_events: list[tuple[str, str, ActivityEventFrame]] = []
        self.parent_bus = parent_bus

    async def ensure(self, scope: RuntimeScope, *, env_overrides=None) -> RunWorker:
        self.ensure_calls.append(scope)
        existing = self.workers.get(scope.worker_identity)
        if existing is not None and existing.running():
            existing.mark_used()
            return existing
        worker = RunWorker(
            scope=scope,
            process=_FakeProcess(),
            inbound_queue=asyncio.Queue(),
            created_at=time.time(),
            last_used_at=time.time(),
        )
        self.workers[scope.worker_identity] = worker
        return worker

    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
        if isinstance(frame, ActivityEventFrame):
            self.sent_activity_events.append((scope_key, conversation_id, frame))
            if self.parent_bus is not None:
                self.parent_bus.push(frame.event)
            return True
        if isinstance(frame, RunStartFrame):
            self.sent_run_starts.append((scope_key, conversation_id, frame))
            return True
        return True

    async def shutdown(self, scope_key: str, conversation_id: str = "") -> bool:
        self.shutdown_calls.append((scope_key, conversation_id or ""))
        worker = self.workers.pop((scope_key, conversation_id or ""), None)
        if worker is None:
            return False
        worker.process.returncode = -15
        return True

    async def shutdown_all(self) -> None:
        self.shutdown_all_called = True
        for worker in self.workers.values():
            worker.process.returncode = -15
        self.workers.clear()


class _Ids:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


class _FakeLeaderAgent:
    def __init__(self, db: CliSessionStore, bus: ActivityEventBus) -> None:
        self._session_db = db
        self.activity_event_bus = bus


@pytest.fixture
def db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


@pytest.fixture
def parent_bus() -> ActivityEventBus:
    return ActivityEventBus()


@pytest_asyncio.fixture
async def harness(db: CliSessionStore, parent_bus: ActivityEventBus, monkeypatch: pytest.MonkeyPatch):
    supervisor = _FakeSupervisor(parent_bus)
    pool = WorkerLeaseManager(supervisor, reap_tick_s=60)
    published_events: list[dict[str, Any]] = []

    def publish_event(payload: dict[str, Any], **kwargs: Any) -> list[Any]:
        published_events.append({"payload": payload, "kwargs": kwargs})
        return []

    router = WorkerFrameRouter(
        sender=supervisor,
        publish_event=publish_event,
        publish_run_terminal=lambda **kwargs: {},
    )
    from tui_gateway import server

    monkeypatch.setattr(server, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(server, "_db_for_stable_session", lambda _stable: db, raising=False)
    try:
        yield SimpleNamespace(
            db=db,
            pool=pool,
            supervisor=supervisor,
            router=router,
            published_events=published_events,
            parent_bus=parent_bus,
        )
    finally:
        await pool.shutdown()


def _profile_context(tmp_path: Path, profile_id: str = "profile-main") -> dict[str, Any]:
    return {
        "agent_profile_id": profile_id,
        "hermes_home": str(tmp_path / profile_id),
    }


def _seed_profile(db: CliSessionStore, tmp_path: Path, profile_id: str) -> None:
    db.profiles.upsert_agent_profile(
        profile_id=profile_id,
        slug=profile_id,
        name=profile_id,
        hermes_home_path=str(tmp_path / profile_id),
        current_version_id=f"version-{profile_id}",
        current_version_number=1,
    )


def _messages(db: CliSessionStore, session_id: str) -> list[dict[str, Any]]:
    return db.messages.all_as_conversation(session_id)


async def _submit_plain_chat(
    db: CliSessionStore,
    pool: WorkerLeaseManager,
    *,
    conversation_id: str,
    text: str,
    reply: str,
    profile_context: dict[str, Any],
) -> None:
    try:
        db.sessions.create(conversation_id, source="tui")
    except Exception:
        pass
    lease = await pool.get_or_spawn(conversation_id, profile_context)
    try:
        db.messages.append(conversation_id, role="user", content=text)
        db.messages.append(conversation_id, role="assistant", content=reply)
    finally:
        await pool.release(lease.conversation_id)


async def _complete_dispatched_run(
    db: CliSessionStore,
    router: WorkerFrameRouter,
    *,
    scope_key: str,
    conversation_id: str | None = None,
    run_id: str,
    conversation_session_id: str,
    text: str,
    usage: dict[str, Any] | None = None,
) -> None:
    try:
        db.sessions.create(conversation_session_id, source="worker")
    except Exception:
        pass
    db.messages.append(
        conversation_session_id,
        role="assistant",
        content=text,
        metadata={"usage": usage or {"total_tokens": 1}},
    )
    await router.on_event(
        scope_key,
        conversation_id or conversation_session_id,
        EventFrame(
            params={
                "type": "message.complete",
                "run_id": run_id,
                "payload": {
                    "text": text,
                    "usage": usage or {"total_tokens": 1},
                },
            }
        ),
    )
    await router.on_run_terminal(
        scope_key,
        conversation_id or conversation_session_id,
        RunTerminalFrame(run_id=run_id, conversation_session_id=conversation_session_id, status="completed"),
    )


@pytest.mark.asyncio
async def test_e2e_plain_chat_via_worker_pool(harness, tmp_path: Path) -> None:
    await _submit_plain_chat(
        harness.db,
        harness.pool,
        conversation_id="conv-A",
        text="hello",
        reply="world",
        profile_context=_profile_context(tmp_path),
    )

    first_worker = harness.supervisor.workers[("profile:profile-main", "conv-A")]
    await _submit_plain_chat(
        harness.db,
        harness.pool,
        conversation_id="conv-A",
        text="hello again",
        reply="world again",
        profile_context=_profile_context(tmp_path),
    )

    messages = _messages(harness.db, "conv-A")
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "hello"),
        ("assistant", "world"),
        ("user", "hello again"),
        ("assistant", "world again"),
    ]
    assert harness.supervisor.workers[("profile:profile-main", "conv-A")] is first_worker
    assert len(harness.supervisor.ensure_calls) == 1
    assert harness.pool.stats()["workerCount"] == 1


@pytest.mark.asyncio
async def test_e2e_team_mission_member_chat(
    harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    team_mission = team_mission_gateway()
    monkeypatch.setattr(team_mission, "_get_db", lambda: harness.db)
    captured_submit: dict[str, Any] = {}
    monkeypatch.setattr(
        team_mission,
        "_proxy_run_submit_via_worker",
        lambda params: captured_submit.update(params) or {"ok": True},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    members = [
        {
            "member_id": "member-leader",
            "profile_id": "profile-leader",
            "profile_version_id": "version-leader",
            "role": "lead",
            "display_name": "Leader",
            "runtime_scope_key": "profile:leader",
            "dovie_profile": {
                "id": "profile-leader",
                "agentProfileVersionId": "version-leader",
                "runtimeScopeKey": "profile:leader",
                "hermesHomePath": str(tmp_path / "leader-home"),
            },
        },
        {
            "member_id": "member-alice",
            "profile_id": "profile-alice",
            "profile_version_id": "version-alice",
            "role": "builder",
            "display_name": "Alice",
            "runtime_scope_key": "profile:alice",
            "dovie_profile": {
                "id": "profile-alice",
                "agentProfileVersionId": "version-alice",
                "runtimeScopeKey": "profile:alice",
                "hermesHomePath": str(tmp_path / "alice-home"),
            },
        },
        {
            "member_id": "member-bob",
            "profile_id": "profile-bob",
            "profile_version_id": "version-bob",
            "role": "qa",
            "display_name": "Bob",
            "runtime_scope_key": "profile:bob",
            "dovie_profile": {
                "id": "profile-bob",
                "agentProfileVersionId": "version-bob",
                "runtimeScopeKey": "profile:bob",
                "hermesHomePath": str(tmp_path / "bob-home"),
            },
        },
    ]
    harness.db.initialize_team_mission_from_strategy(
        mission_id="mission-B",
        conversation_id="conversation-B",
        team_id="team-B",
        title="Team mission",
        objective="Inspect the release",
        workspace_id="workspace-B",
        workspace_path=str(workspace),
        mode="supervised_mission",
        leader_session_id="conv-B",
        metadata={"conversation_session_id": "conv-B"},
        members=members,
    )

    response = team_mission._methods["team_mission.message.submit"](
        1,
        {
            "mission_id": "mission-B",
            "conversation_id": "conversation-B",
            "conversation_session_id": "conv-B",
            "team_id": "team-B",
            "workspace": {"workspace_id": "workspace-B", "workspace_path": str(workspace)},
            "text": "@Alice please review the release plan.",
            "target_member_id": "member-alice",
            "members": members,
        },
    )
    harness.db.messages.append(
        "conv-B",
        role="assistant",
        content="I will review the release plan as Alice.",
        metadata={"participant_id": "member:member-alice", "display_name": "Alice"},
    )
    harness.db.messages.append(
        "conv-B",
        role="assistant",
        content="Bob sees one risk in the test plan.",
        metadata={"participant_id": "member:member-bob", "display_name": "Bob"},
    )

    assert "error" not in response
    run_context = json.loads(captured_submit["run_context_json"])
    assert run_context["conversation_session_id"] == "conv-B"
    assert run_context["participant_id"] == "member:member-alice"
    assert captured_submit["conversation_session_id"] == "conv-B"
    assert captured_submit["runtime_scope_key"] == "member-chat:conversation-B:member-alice"

    messages = _messages(harness.db, "conv-B")
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "@Alice please review the release plan."
    participants = harness.db.participants.list_conversation_participants("conv-B")
    projection = project_participant_transcript(
        messages,
        viewing_participant_id="member:member-alice",
        participants=participants,
    )
    assert [message["role"] for message in projection] == [
        "user", "assistant", "user"
    ]
    assert projection[0]["content"] == "@Alice please review the release plan."
    assert projection[1]["content"] == "I will review the release plan as Alice."
    assert projection[2]["content"] == (
        "[assistant | Bob | member:member-bob]\n"
        "Bob sees one risk in the test plan."
    )
    assert "name" not in projection[2]
    assert projection[2]["metadata"]["speaker_projected_role"] == "user"


@pytest.mark.asyncio
async def test_e2e_async_agent_dispatch_round_trip(harness, tmp_path: Path) -> None:
    from tui_gateway.methods.dispatch import dispatch_agent_async

    _seed_profile(harness.db, tmp_path, "profile-worker")
    result = await dispatch_agent_async(
        {
            "target_profile_id": "profile-worker",
            "prompt": "Investigate the failure",
            "summary": "Investigate failure",
            "parent_conversation_id": "conv-C",
            "_parent_scope_key": "conv-C",
        },
        db=harness.db,
        pool=harness.pool,
        supervisor=harness.supervisor,
        router=harness.router,
        uuid_factory=_Ids("act-C", "conv-C-child", "run-C", "turn-C"),
        time_fn=lambda: 100.0,
    )

    assert result == {
        "activity_id": "act-C",
        "conversation_id": "conv-C-child",
        "execution_mode": "async",
        "persistent": True,
        "status": "running",
    }
    assert harness.supervisor.sent_run_starts[0][0] == "profile:profile-worker"
    assert harness.supervisor.sent_run_starts[0][1] == "conv-C-child"
    assert harness.supervisor.sent_run_starts[0][2].prompt == "Investigate the failure"

    await _complete_dispatched_run(
        harness.db,
        harness.router,
        scope_key="profile:profile-worker",
        conversation_id="conv-C-child",
        run_id="run-C",
        conversation_session_id="conv-C-child",
        text="Shard fixed and tests are green.",
        usage={"total_tokens": 42},
    )

    activity = harness.db.activities.get("act-C")
    assert activity["status"] == "completed"
    assert activity["result_summary"] == "Shard fixed and tests are green."
    assert json.loads(activity["result_json"])["usage"] == {"total_tokens": 42}
    assert harness.published_events[-1]["payload"]["type"] == "activity.completed"
    assert harness.supervisor.sent_activity_events[-1][0] == "conv-C"
    assert harness.parent_bus.peek_count() == 1

    injected = _drain_activity_events_for_api(_FakeLeaderAgent(harness.db, harness.parent_bus))
    assert len(injected) == 1
    assert "Async activity act-C" in injected[0]["content"]
    assert "Shard fixed and tests are green." in injected[0]["content"]
    assert harness.db.activities.get("act-C")["read_at"] is not None


@pytest.mark.asyncio
async def test_e2e_late_worker_completion_after_activity_cancel_stays_cancelled(harness) -> None:
    harness.db.activities.create(
        activity_id="act-cancel",
        conversation_id="conv-cancel-parent",
        kind="agent_dispatch",
    )
    assert harness.db.activities.update_status("act-cancel", "running", started_at=100.0)
    harness.router.record_run_start(
        scope_key="profile:worker",
        conversation_id="conv-cancel-child",
        run_id="run-cancel",
        conversation_session_id="conv-cancel-child",
        turn_id="turn-cancel",
        dispatch_activity_id="act-cancel",
        activity_kind="agent_dispatch",
        parent_scope_key="conv-cancel-parent",
        parent_conversation_id="conv-cancel-parent",
    )

    assert harness.db.activities.mark_cancelled("act-cancel")
    await _complete_dispatched_run(
        harness.db,
        harness.router,
        scope_key="profile:worker",
        conversation_id="conv-cancel-child",
        run_id="run-cancel",
        conversation_session_id="conv-cancel-child",
        text="Late worker completion must not bounce the UI.",
    )

    activity = harness.db.activities.get("act-cancel")
    assert activity["status"] == "cancelled"
    assert activity["result_summary"] is None
    assert harness.published_events[-1]["payload"]["type"] == "activity.cancelled"
    assert harness.published_events[-1]["payload"]["payload"]["status"] == "cancelled"
    assert harness.supervisor.sent_activity_events[-1][2].event["status"] == "cancelled"


@pytest.mark.asyncio
async def test_e2e_async_team_dispatch_round_trip(harness) -> None:
    from tui_gateway.methods.dispatch import dispatch_team_async

    def fake_team_mission_create(_rid: str, params: dict[str, Any]) -> dict[str, Any]:
        harness.db.sessions.create("mission-D", source="team_mission")
        return {
            "jsonrpc": "2.0",
            "id": _rid,
            "result": {
                "mission_id": params["mission_id"],
                "conversation_id": params["mission_id"],
            },
        }

    result = await dispatch_team_async(
        {
            "target_team_id": "team-D",
            "mission_objective": "Complete the release checklist",
            "summary": "Release checklist",
            "parent_conversation_id": "conv-D",
            "_parent_scope_key": "conv-D",
        },
        db=harness.db,
        team_mission_create=fake_team_mission_create,
        uuid_factory=_Ids("act-D", "mission-D"),
        time_fn=lambda: 200.0,
    )
    harness.router.record_run_start(
        scope_key="mission-D",
        conversation_id="mission-D",
        run_id="run-D",
        conversation_session_id="mission-D",
        turn_id="turn-D",
        dispatch_activity_id="act-D",
        activity_kind="team_dispatch",
        parent_scope_key="conv-D",
    )

    assert result == {
        "activity_id": "act-D",
        "mission_id": "mission-D",
        "execution_mode": "async",
        "persistent": True,
        "status": "running",
    }
    assert harness.db.activities.get("act-D")["target_mission_id"] == "mission-D"

    await _complete_dispatched_run(
        harness.db,
        harness.router,
        scope_key="mission-D",
        run_id="run-D",
        conversation_session_id="mission-D",
        text="Team mission completed.",
    )

    activity = harness.db.activities.get("act-D")
    assert activity["status"] == "completed"
    assert activity["target_mission_id"] == "mission-D"
    assert activity["result_summary"] == "Team mission completed."
    assert harness.supervisor.sent_activity_events[-1][0] == "conv-D"


@pytest.mark.asyncio
async def test_e2e_multi_activity_per_conversation(
    harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tui_gateway import server
    from tui_gateway.methods.dispatch import dispatch_agent_async

    _seed_profile(harness.db, tmp_path, "profile-worker")
    harness.db.sessions.create("conv-E", source="tui")
    harness.db.messages.append("conv-E", role="user", content="Dispatch three async tasks.")
    harness.db.session_index.upsert(
        session_id="conv-E",
        title="Concurrent dispatch",
        preview="dispatch",
        source="tui",
        conversation_id="conv-E",
        message_count=1,
        started_at=1.0,
        updated_at=1.0,
    )

    dispatches = await asyncio.gather(
        *(
            dispatch_agent_async(
                {
                    "target_profile_id": "profile-worker",
                    "prompt": f"Task {idx}",
                    "summary": f"Task {idx}",
                    "parent_conversation_id": "conv-E",
                    "_parent_scope_key": "conv-E",
                },
                db=harness.db,
                pool=harness.pool,
                supervisor=harness.supervisor,
                router=harness.router,
                uuid_factory=_Ids(f"act-E{idx}", f"conv-E-child-{idx}", f"run-E{idx}", f"turn-E{idx}"),
                time_fn=lambda idx=idx: 300.0 + idx,
            )
            for idx in range(1, 4)
        )
    )
    await asyncio.gather(
        *(
            _complete_dispatched_run(
                harness.db,
                harness.router,
                scope_key="profile:profile-worker",
                conversation_id=result["conversation_id"],
                run_id=f"run-E{idx}",
                conversation_session_id=result["conversation_id"],
                text=f"Task {idx} complete.",
            )
            for idx, result in enumerate(dispatches, start=1)
        )
    )

    rows = harness.db.activities.list("conv-E")
    assert [row["activity_id"] for row in rows] == ["act-E1", "act-E2", "act-E3"]
    assert {row["status"] for row in rows} == {"completed"}
    assert harness.db.activities.unread_count(conversation_id="conv-E") == 3
    injected = _drain_activity_events_for_api(_FakeLeaderAgent(harness.db, harness.parent_bus))
    assert [message["role"] for message in injected] == ["system", "system", "system"]
    assert [message["content"].splitlines()[0].split()[2] for message in injected] == [
        "act-E1",
        "act-E2",
        "act-E3",
    ]
    assert [row["activity_id"] for row in harness.db.activities.list_unread(conversation_id="conv-E")] == []
