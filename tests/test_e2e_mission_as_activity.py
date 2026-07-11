from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


CONVERSATION_ID = "conversation-1"
CONVERSATION_SESSION_ID = "team-session-1"
TEAM_ID = "team-1"
PARTICIPANT_ID = "member:member-alpha"


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
    monkeypatch.setitem(
        server._methods,
        "run.submit",
        lambda rid, params: {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "run_id": params.get("run_id") or "run-leader",
                "conversation_session_id": params.get("conversation_session_id") or "",
                "runtime_scope_key": params.get("runtime_scope_key") or "",
                "status": "running",
            },
        },
    )
    return server


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response
    return response["result"]


def _workspace(tmp_path: Path, mission_id: str) -> dict[str, str]:
    path = tmp_path / f"workspace-{mission_id}"
    path.mkdir(exist_ok=True)
    return {"workspace_id": f"workspace-{mission_id}", "workspace_path": str(path)}


def _seed_team(db: CliSessionStore, tmp_path: Path) -> None:
    db.profiles.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader",
        description="Plans and verifies work.",
        category="product",
        tags=["planning"],
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "leader-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id="version-leader",
        current_version_number=1,
    )
    db.profiles.upsert_agent_profile(
        profile_id="profile-alpha",
        slug="alpha",
        name="Alpha",
        description="Builds mission work.",
        category="engineering",
        tags=["engineering"],
        hermes_profile_name="alpha",
        hermes_home_path=str(tmp_path / "alpha-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id="version-alpha",
        current_version_number=1,
    )
    db.teams.upsert_agent_team(
        team_id=TEAM_ID,
        name="Mission Activity Team",
        description="Team used by mission-as-activity E2E tests.",
        lead_agent_profile_id="profile-leader",
    )
    db.teams.upsert_agent_team_member(
        member_id="member-leader",
        team_id=TEAM_ID,
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning"],
        profile_name="Leader",
    )
    db.teams.upsert_agent_team_member(
        member_id="member-alpha",
        team_id=TEAM_ID,
        agent_profile_id="profile-alpha",
        agent_profile_version_id="version-alpha",
        role="builder",
        capability_tags=["engineering"],
        profile_name="Alpha",
    )


def _ensure_session(db: CliSessionStore) -> None:
    db.sessions.create(CONVERSATION_SESSION_ID, source="team_mission", transient=False)


def _create_mission(gateway_server: Any, db: CliSessionStore, tmp_path: Path, mission_id: str) -> dict[str, Any]:
    _ensure_session(db)
    response = gateway_server._methods["team_mission.create"](
        f"create-{mission_id}",
        {
            "mission_id": mission_id,
            "conversation_id": CONVERSATION_ID,
            "conversation_session_id": CONVERSATION_SESSION_ID,
            "team_id": TEAM_ID,
            "title": f"Mission {mission_id}",
            "objective": f"Complete {mission_id}.",
            "mode": "supervised_mission",
            "workspace": _workspace(tmp_path, mission_id),
            "metadata": {"start_leader": False},
            "record_user_task_message": False,
        },
    )
    return _assert_ok(response)


def _create_missions(
    gateway_server: Any,
    db: CliSessionStore,
    tmp_path: Path,
    mission_ids: tuple[str, ...] = ("mission-A", "mission-B"),
) -> None:
    _seed_team(db, tmp_path)
    for mission_id in mission_ids:
        _create_mission(gateway_server, db, tmp_path, mission_id)


def _bind_member_run(
    db: CliSessionStore,
    *,
    mission_id: str,
    run_id: str,
    node_id: str = "shared-member-node",
    participant_id: str = PARTICIPANT_ID,
    status: str = "running",
) -> None:
    session_id = f"team:{mission_id}:node:{node_id}"
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind="worker",
        title=f"Worker {mission_id}",
        objective=f"Execute {mission_id}.",
        status="running",
        runtime_scope_key=session_id,
        metadata={"participant_id": participant_id},
    )
    db.runs.upsert(
        run_id=run_id,
        session_id=session_id,
        execution_session_id=f"runtime-{run_id}",
        runtime_scope_key=session_id,
        status=status,
    )
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=session_id,
        execution_session_id=f"runtime-{run_id}",
        runtime_scope_key=session_id,
        role="worker",
        metadata={"participant_id": participant_id},
    )


def _message_complete(run_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "message.complete",
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "seq": 1,
        "payload": {"text": text, "status": "complete"},
    }


def _install_run_cancel_fake(monkeypatch: pytest.MonkeyPatch, db: CliSessionStore) -> list[dict[str, Any]]:
    canceled: list[dict[str, Any]] = []

    def fake_run_cancel(rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        canceled.append(dict(params))
        db.runs.upsert(
            run_id=params["run_id"],
            session_id=params["conversation_session_id"],
            execution_session_id=params["execution_session_id"],
            runtime_scope_key=params["runtime_scope_key"],
            status="cancelled",
        )
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "cancelled", **params}}

    monkeypatch.setitem(server._methods, "run.cancel", fake_run_cancel)
    return canceled


def _cancel_mission(
    gateway_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    db: CliSessionStore,
    mission_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    canceled = _install_run_cancel_fake(monkeypatch, db)
    response = gateway_server._methods["team_mission.cancel"](
        f"cancel-{mission_id}",
        {"mission_id": mission_id, "canceled_by": "user", "reason": "stop"},
    )
    return _assert_ok(response), canceled


def _render(gateway_server: Any) -> dict[str, Any]:
    response = gateway_server._methods["conversation.render_snapshot"](
        "render",
        {
            "conversation_id": CONVERSATION_ID,
            "session_id": CONVERSATION_SESSION_ID,
            "includeRunEvents": True,
        },
    )
    return _assert_ok(response)


def test_e2e_team_mission_create_inserts_kind_mission_activity(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    _seed_team(db, tmp_path)
    result = _create_mission(gateway, db, tmp_path, "mission-A")

    activity = db.activities.get_for_mission("mission-A")
    conversation = db.resolve_team_mission_conversation(CONVERSATION_ID)["conversation"]

    assert result["mission_id"] == "mission-A"
    assert activity is not None
    assert activity["activity_id"] == "mission:mission-A"
    assert activity["conversation_id"] == CONVERSATION_SESSION_ID
    assert activity["kind"] == "mission"
    assert activity["target_mission_id"] == "mission-A"
    assert activity["status"] == "running"
    assert conversation["active_mission_id"] == "mission-A"


def test_e2e_mission_cancel_marks_activity_cancelled_conversation_stays_running_if_other_runs(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    _create_missions(gateway, db, tmp_path)
    _bind_member_run(db, mission_id="mission-A", run_id="run-A")
    _bind_member_run(db, mission_id="mission-B", run_id="run-B")
    db.session_index.upsert(
        session_id=CONVERSATION_SESSION_ID,
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
        status="running",
        running=True,
        active_run_id="run-B",
        active_execution_session_id="runtime-run-B",
        team_id=TEAM_ID,
        mission_id="mission-A",
        conversation_id=CONVERSATION_ID,
    )

    result, canceled = _cancel_mission(gateway, monkeypatch, db, "mission-A")

    row = db.session_index.get(CONVERSATION_SESSION_ID)
    assert result["mission_status"] == "cancelled"
    assert [item["run_id"] for item in canceled] == ["run-A"]
    assert db.activities.get_for_mission("mission-A")["status"] == "cancelled"
    assert db.activities.get_for_mission("mission-B")["status"] == "running"
    assert db.runs.get("run-A")["status"] == "cancelled"
    assert db.runs.get("run-B")["status"] == "running"
    assert row is not None
    assert row["running"] is True
    assert row["status"] == "running"
    active_conversation_run = db.runs.get(row["active_run_id"])
    assert active_conversation_run is not None
    assert active_conversation_run["session_id"] == CONVERSATION_SESSION_ID
    assert active_conversation_run["status"] == "running"


def test_e2e_multi_parallel_missions_in_same_conversation(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    _create_missions(gateway, db, tmp_path)

    active_activities = db.activities.list_active_missions(CONVERSATION_SESSION_ID)
    conversation_missions = db.list_conversation_missions(CONVERSATION_ID)
    conversation = db.resolve_team_mission_conversation(CONVERSATION_ID)["conversation"]

    assert [row["target_mission_id"] for row in active_activities] == ["mission-A", "mission-B"]
    assert {row["mission_id"]: row["status"] for row in conversation_missions} == {
        "mission-A": "active",
        "mission-B": "active",
    }
    assert conversation["conversation_session_id"] == CONVERSATION_SESSION_ID
    assert conversation["active_mission_id"] == "mission-B"


def test_e2e_render_snapshot_returns_missions_top_level(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    _create_missions(gateway, db, tmp_path)
    db.messages.append(CONVERSATION_SESSION_ID, role="user", content="Keep the conversation visible.")

    snapshot = _render(gateway)

    assert snapshot["kind"] == "team_mission"
    assert snapshot["conversation"]["conversation_id"] == CONVERSATION_ID
    assert snapshot["mission"]["mission_id"] == "mission-B"
    assert [row["target_mission_id"] for row in snapshot["missions"]] == ["mission-A", "mission-B"]
    assert {row["kind"] for row in snapshot["missions"]} == {"mission"}


def test_e2e_member_runs_under_different_missions_have_distinct_node_ids_same_participant(
    gateway,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    _create_missions(gateway, db, tmp_path)
    _bind_member_run(db, mission_id="mission-A", run_id="run-A")
    _bind_member_run(db, mission_id="mission-B", run_id="run-B")

    db.append_team_mission_run_event(
        mission_id="mission-A",
        run_id="run-A",
        event=_message_complete("run-A", "mission A done"),
    )
    db.append_team_mission_run_event(
        mission_id="mission-B",
        run_id="run-B",
        event=_message_complete("run-B", "mission B done"),
    )

    node_a = db.get_team_mission_node("mission-A", "shared-member-node")
    node_b = db.get_team_mission_node("mission-B", "shared-member-node")
    events = {
        event["run_id"]: event
        for event in db.runs.list_events("team:mission-A:node:shared-member-node")
    }
    events.update({
        event["run_id"]: event
        for event in db.runs.list_events("team:mission-B:node:shared-member-node")
    })

    assert node_a["canonical_node_id"] == "mission-A:shared-member-node"
    assert node_b["canonical_node_id"] == "mission-B:shared-member-node"
    assert node_a["canonical_node_id"] != node_b["canonical_node_id"]
    assert events["run-A"]["participant_id"] == PARTICIPANT_ID
    assert events["run-B"]["participant_id"] == PARTICIPANT_ID


def test_e2e_active_mission_id_field_still_set_for_backwards_compat_but_activities_authoritative(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
    db: CliSessionStore,
    tmp_path: Path,
) -> None:
    _create_missions(gateway, db, tmp_path)

    before = db.resolve_team_mission_conversation(CONVERSATION_ID)["conversation"]
    before_activities = db.activities.list_active_missions(CONVERSATION_SESSION_ID)
    assert before["active_mission_id"] == "mission-B"
    assert [row["target_mission_id"] for row in before_activities] == ["mission-A", "mission-B"]

    _cancel_mission(gateway, monkeypatch, db, "mission-A")

    after = db.resolve_team_mission_conversation(CONVERSATION_ID)["conversation"]
    after_activities = db.activities.list_active_missions(CONVERSATION_SESSION_ID)
    assert after["active_mission_id"] == "mission-B"
    assert [row["target_mission_id"] for row in after_activities] == ["mission-B"]
    assert db.activities.get_for_mission("mission-A")["status"] == "cancelled"
