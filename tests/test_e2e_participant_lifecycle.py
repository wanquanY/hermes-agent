from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from hermes_team_mission.gateway import runtime_methods
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway.run_worker import EventFrame
from tui_gateway.services import run_control
from hermes_agent.orchestration.worker_frame_router import WorkerFrameRouter


class _FakeSender:
    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
        return True


class _CaptureTransport:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, obj: dict[str, Any]) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        return None


@pytest.fixture
def db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


@pytest.fixture
def gateway_modules(monkeypatch: pytest.MonkeyPatch, db: CliSessionStore):
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    importlib.import_module("tui_gateway.methods.session_history")
    team_mission = team_mission_gateway()

    monkeypatch.setattr(server, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(server, "_db_for_stable_session", lambda _stable: db, raising=False)
    monkeypatch.setattr(server, "_resolve_model", lambda: "gpt-test", raising=False)
    monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(session_methods, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(
        session_methods,
        "_bind_session_workspace",
        lambda **kwargs: kwargs.get("workspace") or {},
        raising=False,
    )
    monkeypatch.setattr(team_mission, "_get_db", lambda: db, raising=False)
    return server, team_mission


@pytest.fixture
def seeded_team(db: CliSessionStore, tmp_path: Path) -> dict[str, Any]:
    _seed_team(db, tmp_path)
    return {
        "team_id": "team-1",
        "conversation_id": "conversation-1",
        "conversation_session_id": "team-session-1",
        "mission_id": "mission-1",
        "workspace": _workspace(tmp_path),
    }


def _workspace(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "workspace"
    path.mkdir(exist_ok=True)
    return {"workspace_id": "workspace-1", "workspace_path": str(path)}


def _seed_profile(db: CliSessionStore, tmp_path: Path, profile_id: str, name: str) -> None:
    db.profiles.upsert_agent_profile(
        profile_id=profile_id,
        slug=profile_id,
        name=name,
        avatar=f"avatar://{profile_id}",
        hermes_profile_name=profile_id,
        hermes_home_path=str(tmp_path / f"{profile_id}-home"),
        current_version_id=f"version-{profile_id}",
        current_version_number=1,
    )


def _seed_team(db: CliSessionStore, tmp_path: Path) -> None:
    _seed_profile(db, tmp_path, "profile-leader", "Leader")
    for profile_id, name in (
        ("profile-alpha", "Alpha"),
        ("profile-beta", "Beta"),
        ("profile-gamma", "Gamma"),
    ):
        _seed_profile(db, tmp_path, profile_id, name)
    db.teams.upsert_agent_team(
        team_id="team-1",
        name="Team One",
        description="Participant lifecycle test team.",
        lead_agent_profile_id="profile-leader",
    )
    for member_id, profile_id, role, name in (
        ("member-alpha", "profile-alpha", "builder", "Alpha"),
        ("member-beta", "profile-beta", "reviewer", "Beta"),
        ("member-gamma", "profile-gamma", "qa", "Gamma"),
    ):
        db.teams.upsert_agent_team_member(
            member_id=member_id,
            team_id="team-1",
            agent_profile_id=profile_id,
            agent_profile_version_id=f"version-{profile_id}",
            role=role,
            profile_name=name,
            profile_avatar=f"avatar://{profile_id}",
        )


def _participants_by_id(db: CliSessionStore, session_id: str) -> dict[str, dict[str, Any]]:
    return {row["participant_id"]: row for row in db.participants.list_conversation_participants(session_id)}


def _create_team_conversation(
    team_mission: Any,
    seeded_team: dict[str, Any],
) -> dict[str, Any]:
    response = team_mission._methods["team_mission.create"](
        1,
        {
            "mission_id": seeded_team["mission_id"],
            "conversation_id": seeded_team["conversation_id"],
            "conversation_session_id": seeded_team["conversation_session_id"],
            "team_id": seeded_team["team_id"],
            "title": "Team work",
            "objective": "Ship the participant lifecycle",
            "mode": "supervised_mission",
            "workspace": seeded_team["workspace"],
            "conversation_only": True,
            "metadata": {"start_leader": False},
        },
    )
    assert "error" not in response
    return response["result"]


def _team_render_response(server: Any, seeded_team: dict[str, Any]) -> dict[str, Any]:
    return server._methods["conversation.render_snapshot"](
        1,
        {
            "kind": "team_mission",
            "conversation_id": seeded_team["conversation_id"],
            "session_id": seeded_team["conversation_session_id"],
            "includeRunEvents": True,
        },
    )


def _install_team_resolver(monkeypatch: pytest.MonkeyPatch, server: Any, seeded_team: dict[str, Any]) -> None:
    monkeypatch.setitem(
        server._methods,
        "team_mission.conversation.resolve",
        lambda rid, params: {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "conversation": {
                    "conversation_id": seeded_team["conversation_id"],
                    "conversation_session_id": seeded_team["conversation_session_id"],
                    "team_id": seeded_team["team_id"],
                },
                "mission": {"mission_id": seeded_team["mission_id"], "team_id": seeded_team["team_id"]},
                "team": {"id": seeded_team["team_id"], "name": "Team One"},
                "graph": {},
            },
        },
    )


def _published_payloads(transport: _CaptureTransport) -> list[dict[str, Any]]:
    return [
        frame["params"]
        for frame in transport.frames
        if frame.get("method") == "event" and isinstance(frame.get("params"), dict)
    ]


def _participant_id(message: dict[str, Any]) -> str:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    team_mission = metadata.get("team_mission") if isinstance(metadata.get("team_mission"), dict) else {}
    return str(
        message.get("participant_id")
        or message.get("participantId")
        or metadata.get("participant_id")
        or metadata.get("participantId")
        or team_mission.get("participant_id")
        or team_mission.get("participantId")
        or ""
    )


def test_e2e_session_create_full_flow(gateway_modules, db: CliSessionStore, tmp_path: Path) -> None:
    server, _team_mission = gateway_modules

    response = server.handle_request(
        {
            "id": "r1",
            "method": "session.create",
            "params": {
                "control_plane_only": True,
                "agent_profile_id": "profile-direct",
                "agent_profile_name": "Direct Agent",
                "agent_profile_avatar": "avatar://direct",
                "runtime_scope_key": "profile:profile-direct",
                "title": "Direct participant lifecycle",
                "cwd": str(tmp_path),
                "workspace": {"id": "ws-1", "path": str(tmp_path), "kind": "local"},
            },
        }
    )

    assert "error" not in response
    session_id = response["result"]["conversation_session_id"]
    participants = _participants_by_id(db, session_id)
    assert set(participants) == {"user", "agent:profile-direct"}
    assert participants["user"]["role"] == "user"
    assert participants["agent:profile-direct"]["display_name"] == "Direct Agent"

    db.messages.append(session_id, role="user", content="Start participant lifecycle.")
    db.session_index.upsert(
        session_id=session_id,
        owner_agent_profile_id="profile-direct",
        runtime_scope_key="profile:profile-direct",
        title="Direct participant lifecycle",
        source="tui",
        message_count=1,
    )
    index = server._methods["session.index.list"](2, {})
    row = next(item for item in index["result"]["sessions"] if item["id"] == session_id)
    assert row["id"] == session_id
    assert db.participants.get_participant(row["id"], "user")["role"] == "user"

    snapshot = server._methods["conversation.render_snapshot"](3, {"session_id": session_id})
    assert "error" not in snapshot
    assert [row["participant_id"] for row in snapshot["result"]["participants"]] == [
        "user",
        "agent:profile-direct",
    ]


def test_e2e_team_mission_create_full_flow(
    gateway_modules,
    db: CliSessionStore,
    seeded_team: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, team_mission = gateway_modules
    _create_team_conversation(team_mission, seeded_team)
    _install_team_resolver(monkeypatch, server, seeded_team)

    participants = _participants_by_id(db, seeded_team["conversation_session_id"])
    leader_participant_id = f"leader:{seeded_team['team_id']}"
    assert set(participants) == {
        "user",
        f"leader:{seeded_team['conversation_id']}",
        leader_participant_id,
        "member:member-alpha",
        "member:member-beta",
        "member:member-gamma",
    }

    snapshot = _team_render_response(server, seeded_team)
    assert "error" not in snapshot
    assert len(snapshot["result"]["participants"]) == 6
    for member_id in ("member-alpha", "member-beta", "member-gamma"):
        row = participants[f"member:{member_id}"]
        assert row["member_id"] == member_id
        assert row["agent_profile_id"]
        assert row["display_name"]


@pytest.mark.asyncio
async def test_e2e_member_chat_emits_participant_id_in_message(
    gateway_modules,
    db: CliSessionStore,
    seeded_team: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _server, team_mission = gateway_modules
    _create_team_conversation(team_mission, seeded_team)
    captured_submit: dict[str, Any] = {}
    monkeypatch.setattr(
        runtime_methods,
        "_proxy_run_submit_via_worker",
        lambda params: captured_submit.update(params) or {"ok": True},
    )

    response = runtime_methods._submit_message_to_member(
        "rid-member",
        {
            "team_id": seeded_team["team_id"],
            "conversation_id": seeded_team["conversation_id"],
            "conversation_session_id": seeded_team["conversation_session_id"],
            "client_run_id": "run-member-alpha",
            "turn_id": "turn-member-alpha",
            "cwd": seeded_team["workspace"]["workspace_path"],
            "workspace": seeded_team["workspace"],
        },
        db=db,
        target_member_id="member-alpha",
        conversation_id=seeded_team["conversation_id"],
        conversation_session_id=seeded_team["conversation_session_id"],
        mission={
            "mission_id": seeded_team["mission_id"],
            "team_id": seeded_team["team_id"],
            "workspace_path": seeded_team["workspace"]["workspace_path"],
        },
        text="@Alpha please review.",
    )
    assert "error" not in response

    transport = _CaptureTransport()
    run_control.subscribe_session(
        conversation_session_id=seeded_team["conversation_session_id"],
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
        conversation_id=seeded_team["conversation_id"],
        run_id="run-member-alpha",
        conversation_session_id=seeded_team["conversation_session_id"],
        turn_id="turn-member-alpha",
        run_context_json=captured_submit["run_context_json"],
    )

    await router.on_event(
        captured_submit["runtime_scope_key"],
        seeded_team["conversation_id"],
        EventFrame(
            params={
                "type": "message.complete",
                "session_id": seeded_team["conversation_session_id"],
                "conversation_session_id": seeded_team["conversation_session_id"],
                "run_id": "run-member-alpha",
                "turn_id": "turn-member-alpha",
                "seq": 1,
                "payload": {"text": "Alpha reviewed it."},
            }
        ),
    )
    run_control.detach_transport(transport)

    [payload] = [
        payload
        for payload in _published_payloads(transport)
        if payload["type"] == "message.complete" and payload["run_id"] == "run-member-alpha"
    ]
    assert payload["participant_id"] == "member:member-alpha"
    assert payload["payload"]["participant_id"] == "member:member-alpha"
    stored = db.runs.list_events(seeded_team["conversation_session_id"], run_id="run-member-alpha")[0]
    assert stored["participant_id"] == "member:member-alpha"


def test_e2e_render_snapshot_messages_have_participant_id_per_message(
    gateway_modules,
    db: CliSessionStore,
    seeded_team: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, team_mission = gateway_modules
    _create_team_conversation(team_mission, seeded_team)
    _install_team_resolver(monkeypatch, server, seeded_team)
    session_id = seeded_team["conversation_session_id"]

    db.messages.append(
        session_id,
        role="user",
        content="Please coordinate.",
        metadata={"run_id": "run-user", "turn_id": "turn-user"},
    )
    db.runs.append_event(
        session_id,
        {
            "type": "message.complete",
            "session_id": session_id,
            "conversation_session_id": session_id,
            "run_id": "run-user",
            "turn_id": "turn-user",
            "seq": 1,
            "payload": {"text": "Please coordinate."},
        },
        participant_id="user",
    )
    db.messages.append(
        session_id,
        role="assistant",
        content="Leader will coordinate.",
        metadata={"run_id": "run-leader", "turn_id": "turn-leader"},
    )
    db.runs.append_event(
        session_id,
        {
            "type": "message.complete",
            "session_id": session_id,
            "conversation_session_id": session_id,
            "run_id": "run-leader",
            "turn_id": "turn-leader",
            "seq": 2,
            "payload": {"text": "Leader will coordinate."},
        },
        participant_id=f"leader:{seeded_team['team_id']}",
    )
    db.messages.append(
        session_id,
        role="assistant",
        content="Alpha has the build.",
        metadata={"run_id": "run-alpha", "turn_id": "turn-alpha"},
    )
    db.runs.append_event(
        session_id,
        {
            "type": "message.complete",
            "session_id": session_id,
            "conversation_session_id": session_id,
            "run_id": "run-alpha",
            "turn_id": "turn-alpha",
            "seq": 3,
            "payload": {"text": "Alpha has the build."},
        },
        participant_id="member:member-alpha",
    )

    snapshot = _team_render_response(server, seeded_team)

    assert "error" not in snapshot
    messages_by_text = {
        message.get("text"): message
        for message in snapshot["result"]["messages"]
        if message.get("text") in {
            "Please coordinate.",
            "Leader will coordinate.",
            "Alpha has the build.",
        }
    }
    assert _participant_id(messages_by_text["Please coordinate."]) == "user"
    assert _participant_id(messages_by_text["Leader will coordinate."]) == f"leader:{seeded_team['team_id']}"
    assert _participant_id(messages_by_text["Alpha has the build."]) == "member:member-alpha"


def test_e2e_backfill_migration_creates_rows_for_existing_conversations(
    db: CliSessionStore,
    seeded_team: dict[str, Any],
) -> None:
    db.sessions.create("legacy-direct", source="tui")
    db.sessions.create(seeded_team["conversation_session_id"], source="team_mission")
    db.session_index.upsert(
        session_id="legacy-direct",
        owner_agent_profile_id="profile-alpha",
        owner_profile_version_id="version-profile-alpha",
        source="tui",
    )
    db.upsert_team_mission_conversation(
        conversation_id=seeded_team["conversation_id"],
        conversation_session_id=seeded_team["conversation_session_id"],
        team_id=seeded_team["team_id"],
        title="Legacy Team",
    )
    db._conn.execute("DELETE FROM conversation_participants")  # noqa: SLF001

    result = db.participants.reconcile()

    assert result["ran"] is True
    direct = _participants_by_id(db, "legacy-direct")
    team = _participants_by_id(db, seeded_team["conversation_session_id"])
    assert set(direct) == {"user", "agent:profile-alpha"}
    assert len(direct) + len(team) == 8
    assert set(team) == {
        "agent",
        "user",
        "leader:team-1",
        "member:member-alpha",
        "member:member-beta",
        "member:member-gamma",
    }


@pytest.mark.asyncio
async def test_e2e_approval_observer_still_works_with_new_participants_table(
    gateway_modules,
    db: CliSessionStore,
    seeded_team: dict[str, Any],
) -> None:
    _server, team_mission = gateway_modules
    _create_team_conversation(team_mission, seeded_team)
    session_id = seeded_team["conversation_session_id"]
    assert db.participants.list_conversation_participants(session_id)

    router = WorkerFrameRouter(
        sender=_FakeSender(),
        publish_event=lambda params, **kwargs: [],
        publish_run_terminal=lambda **kwargs: {},
    )
    await router.on_event(
        "team:conversation-1:leader-conversation",
        seeded_team["conversation_id"],
        EventFrame(
            params={
                "type": "clarify.request",
                "conversation_session_id": session_id,
                "run_id": "run-approval",
                "payload": {"request_id": "clarify-1", "question": "Proceed?"},
            }
        ),
    )

    rows = db.session_index.list(limit=10, include_transient=True)["sessions"]
    row = next(item for item in rows if item["session_id"] == session_id)
    assert row["waiting_approval"] == 1
