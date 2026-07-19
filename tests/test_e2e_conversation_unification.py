from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


@pytest.fixture
def db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, db: CliSessionStore):
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
    return server, team_mission


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response
    return response["result"]


def _workspace(tmp_path: Path, name: str = "workspace") -> dict[str, str]:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return {"workspace_id": name, "workspace_path": str(path)}


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


def _seed_team(db: CliSessionStore, tmp_path: Path, *, team_id: str = "team-1") -> None:
    _seed_profile(db, tmp_path, "profile-leader", "Leader")
    _seed_profile(db, tmp_path, "profile-builder", "Builder")
    db.teams.upsert_agent_team(
        team_id=team_id,
        name="Unified Team",
        description="Conversation unification E2E team.",
        lead_agent_profile_id="profile-leader",
    )
    db.teams.upsert_agent_team_member(
        member_id="member-builder",
        team_id=team_id,
        agent_profile_id="profile-builder",
        agent_profile_version_id="version-profile-builder",
        role="builder",
        profile_name="Builder",
        profile_avatar="avatar://profile-builder",
    )


def _create_direct_session(
    gateway_server: Any,
    db: CliSessionStore,
    tmp_path: Path,
    *,
    session_id_hint: str = "direct-session",
) -> str:
    response = gateway_server.handle_request(
        {
            "id": "direct-create",
            "method": "session.create",
            "params": {
                "session_id": session_id_hint,
                "control_plane_only": True,
                "agent_profile_id": "profile-direct",
                "agent_profile_name": "Direct Agent",
                "runtime_scope_key": "profile:profile-direct",
                "title": "Direct conversation",
                "cwd": str(tmp_path),
                "workspace": {"id": "ws-direct", "path": str(tmp_path), "kind": "local"},
            },
        }
    )
    result = _assert_ok(response)
    session_id = str(result.get("conversation_session_id") or result.get("session_id") or session_id_hint)
    db.messages.append(session_id, role="user", content="direct hello")
    db.session_index.upsert(
        session_id=session_id,
        owner_agent_profile_id="profile-direct",
        runtime_scope_key="profile:profile-direct",
        title="Direct conversation",
        preview="direct hello",
        source="tui",
        session_kind="hermes_session",
        conversation_kind="direct",
        message_count=1,
        started_at=1,
        updated_at=1,
    )
    return session_id


def _create_team_conversation(
    db: CliSessionStore,
    team_mission: Any,
    tmp_path: Path,
    *,
    conversation_id: str = "conversation-team",
    session_id: str = "team-session",
    mission_id: str = "mission-team",
    conversation_only: bool = False,
) -> dict[str, str]:
    params: dict[str, Any] = {
        "mission_id": mission_id,
        "conversation_id": conversation_id,
        "conversation_session_id": session_id,
        "team_id": "team-1",
        "title": "Team conversation",
        "objective": "Unify conversation rendering",
        "mode": "supervised_mission",
        "workspace": _workspace(tmp_path, f"workspace-{session_id}"),
        "metadata": {"start_leader": False},
        "conversation_only": True,
    }
    response = team_mission._methods["team_mission.create"]("team-create", params)
    _assert_ok(response)
    if conversation_only:
        db.upsert_team_mission_conversation(
            conversation_id=conversation_id,
            conversation_session_id=session_id,
            team_id="team-1",
            title="Team conversation",
            active_mission_id="",
            created_at=1,
            updated_at=2,
        )
    else:
        workspace = params["workspace"]
        db.upsert_team_mission(
            mission_id=mission_id,
            conversation_id=conversation_id,
            team_id="team-1",
            title="Team mission",
            objective="Unify conversation rendering",
            workspace_id=workspace["workspace_id"],
            workspace_path=workspace["workspace_path"],
            mode="supervised_mission",
            status="running",
            leader_session_id=session_id,
            metadata={"conversation_session_id": session_id},
        )
        db.upsert_team_mission_conversation(
            conversation_id=conversation_id,
            conversation_session_id=session_id,
            team_id="team-1",
            title="Team conversation",
            active_mission_id=mission_id,
            created_at=1,
            updated_at=2,
        )
    return {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "mission_id": "" if conversation_only else mission_id,
        "team_id": "team-1",
    }


def _message_complete(
    run_id: str,
    text: str,
    *,
    seq: int = 0,
    participant_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text, "status": "complete"}
    if participant_id:
        payload["participant_id"] = participant_id
    return {
        "type": "message.complete",
        "session_id": f"runtime-{run_id}",
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "runtime_scope_key": "team:conversation-team",
        "seq": seq,
        "payload": payload,
    }


def _render(gateway_server: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _assert_ok(
        gateway_server._methods["conversation.render_snapshot"](
            "render",
            {"includeRunEvents": True, **params},
        )
    )


def test_e2e_direct_conversation_creates_routes_renders_correctly_kind_direct(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    gateway_server, _team_mission = gateway
    session_id = _create_direct_session(gateway_server, db, tmp_path)

    snapshot = _render(gateway_server, {"session_id": session_id})

    assert snapshot["kind"] == "ordinary"
    assert snapshot["session_id"] == session_id
    assert snapshot["messages"][0]["text"] == "direct hello"
    assert db.session_index.get(session_id)["conversation_kind"] == "direct"


def test_e2e_team_conversation_creates_routes_renders_correctly_kind_team(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(db, team_mission, tmp_path)
    db.runs.append_event(
        team["session_id"],
        _message_complete("run-leader", "leader complete", participant_id="leader:conversation-team"),
    )

    snapshot = _render(gateway_server, {"session_id": team["session_id"]})

    assert snapshot["kind"] == "team_mission"
    assert snapshot["conversation"]["conversation_session_id"] == team["session_id"]
    assert "conversation_id" not in snapshot["conversation"]
    assert snapshot["mission"]["mission_id"] == team["mission_id"]
    assert snapshot["messages"][0]["text"] == "leader complete"
    assert snapshot["messages"][0]["metadata"]["source"] == "team_mission.runtime_event"
    assert db.session_index.get(team["session_id"])["conversation_kind"] == "team"


def test_e2e_zero_mission_team_conversation_still_renders_after_mission_cancel(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(db, team_mission, tmp_path)
    db.messages.append(team["session_id"], role="user", content="conversation remains visible")

    cancelled = db.cancel_team_mission(
        mission_id=team["mission_id"],
        canceled_by="user",
        reason="E2E cancel",
    )
    assert cancelled["mission_status"] == "cancelled"

    snapshot = _render(gateway_server, {"session_id": team["session_id"]})
    resolved = db.resolve_team_mission_conversation(team["conversation_id"])

    assert snapshot["kind"] == "team_mission"
    assert snapshot["conversation"]["conversation_session_id"] == team["session_id"]
    assert "conversation_id" not in snapshot["conversation"]
    assert snapshot["messages"][0]["text"] == "conversation remains visible"
    assert db.session_index.get(team["session_id"])["conversation_kind"] == "team"
    assert resolved["conversation"]["active_mission_id"] == ""


def test_e2e_sidebar_session_index_only_contains_team_metadata(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(db, team_mission, tmp_path, conversation_only=True)
    db.messages.append(team["session_id"], role="user", content="visible sidebar row")
    db.session_index.upsert(
        session_id=team["session_id"],
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
        team_id=team["team_id"],
        conversation_id=team["conversation_id"],
        title="Team conversation",
        preview="visible sidebar row",
        message_count=1,
        started_at=1,
        updated_at=2,
    )
    monkeypatch.setitem(
        gateway_server._methods,
        "team_mission.conversation.list",
        lambda _rid, _params: pytest.fail("sidebar must not fetch team conversations"),
    )

    result = _assert_ok(
        gateway_server._methods["session.index.list"](
            "sidebar",
            {"includeTransient": True, "conversationKind": "team"},
        )
    )

    assert [item["id"] for item in result["sessions"]] == [team["session_id"]]
    row = result["sessions"][0]
    assert row["conversation_kind"] == "team"
    assert row["team"]["id"] == "team-1"
    assert row["team"]["name"] == "Unified Team"
    assert row["team_name"] == "Unified Team"
    assert row["active_mission_id"] == ""


def test_e2e_run_events_seq_monotonic_across_leader_member_runs(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(db, team_mission, tmp_path)
    db.runs.append_event(
        team["session_id"],
        _message_complete("run-leader", "leader first", seq=1, participant_id="leader:conversation-team"),
    )
    db.runs.append_event(
        team["session_id"],
        _message_complete("run-member", "member second", seq=1, participant_id="member:member-builder"),
    )

    persisted_events = db.runs.list_events(team["session_id"])
    snapshot = _render(gateway_server, {"conversation_id": team["conversation_id"]})

    assert [event["seq"] for event in persisted_events] == [1, 2]
    assert [event["run_id"] for event in persisted_events] == ["run-leader", "run-member"]
    assert [message["text"] for message in snapshot["messages"]] == ["leader first", "member second"]
    assert [message["participant_id"] for message in snapshot["messages"]] == [
        "leader:conversation-team",
        "member:member-builder",
    ]


def test_e2e_team_mission_events_audit_log_separate_from_render(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(db, team_mission, tmp_path)
    db.runs.append_event(
        team["session_id"],
        _message_complete("run-member", "render from run_events", participant_id="member:member-builder"),
    )
    db.append_team_mission_structural_event(
        mission_id=team["mission_id"],
        source_event={
            "type": "message.complete",
            "run_id": "audit-run",
            "seq": 1,
            "payload": {"text": "audit event must not render", "status": "complete"},
        },
        dedupe_key="audit-message",
    )

    snapshot = _render(gateway_server, {"session_id": team["session_id"]})
    audit = _assert_ok(
        gateway_server._methods["team_mission.events"](
            "audit",
            {"mission_id": team["mission_id"]},
        )
    )

    assert [message["text"] for message in snapshot["messages"]] == ["render from run_events"]
    assert "teamMissionEvents" not in snapshot
    assert audit["audit_only"] is True
    assert [event["payload"]["source_event_type"] for event in audit["events"]] == [
        "message.complete"
    ]


def test_e2e_conversation_kind_decoupled_from_active_mission_lifecycle(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    gateway_server, team_mission = gateway
    _seed_team(db, tmp_path)
    team = _create_team_conversation(
        db,
        team_mission,
        tmp_path,
        conversation_id="conversation-lifecycle",
        session_id="team-session-lifecycle",
        mission_id="mission-lifecycle",
    )
    direct_session_id = _create_direct_session(
        gateway_server,
        db,
        tmp_path,
        session_id_hint="direct-with-mission-link",
    )
    db.session_index.upsert(
        session_id=direct_session_id,
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="direct",
        conversation_id=team["conversation_id"],
        mission_id=team["mission_id"],
        title="Direct with historical mission link",
        preview="still direct",
        started_at=2,
        updated_at=2,
    )

    direct_snapshot = _render(
        gateway_server,
        {
            "session_id": direct_session_id,
            "active_mission_id": team["mission_id"],
        },
    )
    db.cancel_team_mission(
        mission_id=team["mission_id"],
        canceled_by="user",
        reason="lifecycle decoupling",
    )
    team_snapshot = _render(gateway_server, {"session_id": team["session_id"]})

    assert direct_snapshot["kind"] == "ordinary"
    assert db.session_index.get(direct_session_id)["conversation_kind"] == "direct"
    assert team_snapshot["kind"] == "team_mission"
    assert db.session_index.get(team["session_id"])["conversation_kind"] == "team"
    resolved = db.resolve_team_mission_conversation(team["conversation_id"])
    assert resolved["conversation"]["active_mission_id"] == ""
