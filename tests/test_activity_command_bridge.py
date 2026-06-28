from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from hermes_state import SessionDB
from hermes_team_mission.runtime.activity_command_bridge import (
    record_legacy_activity_command,
)
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway import server


def _workspace_payload(tmp_path: Path, name: str = "workspace-1") -> dict[str, str]:
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)
    return {"workspace_id": name, "workspace_path": str(workspace)}


def _assert_ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response
    return response["result"]


def _call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return server.handle_request({
        "jsonrpc": "2.0",
        "id": f"test-{method}",
        "method": method,
        "params": params or {},
    })


def _commands(db: SessionDB, activity_id: str) -> list[dict[str, Any]]:
    return db.list_activity_commands_for_activity(activity_id, limit=50)


def _command_by_source(
    db: SessionDB,
    activity_id: str,
    source: str,
) -> dict[str, Any]:
    matches = [
        row for row in _commands(db, activity_id)
        if row.get("metadata", {}).get("source") == source
    ]
    assert len(matches) == 1
    return matches[0]


def _seed_registry_team(db: SessionDB, tmp_path: Path) -> None:
    db.upsert_agent_profile(
        profile_id="profile-leader",
        slug="leader",
        name="Leader",
        description="Plans team work.",
        category="product",
        tags=["planning"],
        hermes_profile_name="leader",
        hermes_home_path=str(tmp_path / "leader-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id="version-leader",
        current_version_number=1,
    )
    db.upsert_agent_profile(
        profile_id="profile-builder",
        slug="builder",
        name="Builder",
        description="Builds team work.",
        category="engineering",
        tags=["build"],
        hermes_profile_name="builder",
        hermes_home_path=str(tmp_path / "builder-home"),
        default_toolsets=["terminal"],
        recommended_skills=[],
        current_version_id="version-builder",
        current_version_number=1,
    )
    db.upsert_agent_team(
        team_id="team-1",
        name="Test Team",
        description="Team for activity command bridge tests.",
        lead_agent_profile_id="profile-leader",
        default_mode="supervised_mission",
        policy={},
    )
    db.upsert_agent_team_member(
        member_id="member-leader",
        team_id="team-1",
        agent_profile_id="profile-leader",
        agent_profile_version_id="version-leader",
        role="lead",
        capability_tags=["planning"],
    )
    db.upsert_agent_team_member(
        member_id="member-builder",
        team_id="team-1",
        agent_profile_id="profile-builder",
        agent_profile_version_id="version-builder",
        role="builder",
        capability_tags=["build"],
    )


@pytest.fixture()
def gateway_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SessionDB:
    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)

    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_db", db, raising=False)
    monkeypatch.setattr(server, "_db_error", None, raising=False)
    monkeypatch.setattr(server, "_db_by_home", {}, raising=False)
    monkeypatch.setattr(server, "_db_error_by_home", {}, raising=False)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_SCOPE_KEY", "profile:profile-leader")

    def fake_run_submit(rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "running",
                "run_id": params.get("run_id") or params.get("client_run_id") or "run-1",
                "turn_id": params.get("turn_id") or "turn-1",
                "session_id": "runtime-1",
                "stored_session_id": params.get("stored_session_id") or params.get("session_id") or "",
                "runtime_scope_key": params.get("runtime_scope_key") or "",
            },
        }

    def fake_run_cancel(rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "status": "cancelled",
                "run_id": params.get("run_id") or "",
                "turn_id": params.get("turn_id") or "",
                "stored_session_id": params.get("stored_session_id") or "",
            },
        }

    monkeypatch.setitem(server._methods, "run.submit", fake_run_submit)
    monkeypatch.setitem(server._methods, "run.cancel", fake_run_cancel)
    try:
        yield db
    finally:
        db.close()


def _create_mission(
    tmp_path: Path,
    *,
    mission_id: str = "mission-1",
    conversation_id: str = "conversation-1",
    conversation_session_id: str = "team-session-1",
) -> dict[str, Any]:
    return _assert_ok(
        _call(
            "team_mission.create",
            {
                "mission_id": mission_id,
                "conversation_id": conversation_id,
                "conversation_session_id": conversation_session_id,
                "team_id": "team-1",
                "title": "Test mission",
                "objective": "Validate activity command bridge.",
                "mode": "supervised_mission",
                "workspace": _workspace_payload(tmp_path),
                "metadata": {"start_leader": False},
                "record_user_task_message": False,
            },
        )
    )


class _RecordingDB:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def insert_activity_command(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return kwargs


def test_record_legacy_activity_command_returns_command_id_on_success() -> None:
    db = _RecordingDB()

    command_id = record_legacy_activity_command(
        db,
        activity_id="mission:mission-1",
        kind="create",
        payload={"mission_id": "mission-1"},
        source="team_mission.create",
    )

    assert command_id.startswith("legacy-create-")
    assert db.calls[0]["command_id"] == command_id


def test_record_legacy_activity_command_returns_empty_on_invalid_activity_id() -> None:
    assert (
        record_legacy_activity_command(
            _RecordingDB(),
            activity_id=" ",
            kind="create",
            source="team_mission.create",
        )
        == ""
    )


def test_record_legacy_activity_command_returns_empty_on_invalid_kind() -> None:
    assert (
        record_legacy_activity_command(
            _RecordingDB(),
            activity_id="mission:mission-1",
            kind="restart",
            source="team_mission.node.start",
        )
        == ""
    )


def test_record_legacy_activity_command_returns_empty_when_db_missing_insert_method() -> None:
    assert (
        record_legacy_activity_command(
            object(),
            activity_id="mission:mission-1",
            kind="create",
            source="team_mission.create",
        )
        == ""
    )


def test_record_legacy_activity_command_swallows_insert_exception() -> None:
    class BrokenDB:
        def insert_activity_command(self, **_kwargs: Any) -> None:
            raise RuntimeError("insert failed")

    assert (
        record_legacy_activity_command(
            BrokenDB(),
            activity_id="mission:mission-1",
            kind="create",
            source="team_mission.create",
        )
        == ""
    )


def test_record_legacy_activity_command_persists_source_in_metadata() -> None:
    db = _RecordingDB()

    record_legacy_activity_command(
        db,
        activity_id="mission:mission-1",
        kind="cancel",
        source="team_mission.cancel",
    )

    assert db.calls[0]["metadata"]["source"] == "team_mission.cancel"


def test_record_legacy_activity_command_persists_via_legacy_bridge_marker() -> None:
    db = _RecordingDB()

    record_legacy_activity_command(
        db,
        activity_id="chat:session-1",
        kind="start",
        source="prompt.submit",
    )

    assert db.calls[0]["metadata"]["via"] == "legacy_bridge"


def test_team_mission_create_records_activity_command(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    _create_mission(tmp_path)

    row = _command_by_source(gateway_db, "mission:mission-1", "team_mission.create")
    assert row["kind"] == "create"
    assert row["state"] == "accepted"
    assert row["payload"]["mission_id"] == "mission-1"
    assert row["payload"]["team_id"] == "team-1"


def test_team_mission_node_start_records_activity_command(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    graph = _create_mission(tmp_path)
    node = next(item for item in graph["graph"]["nodes"] if item["kind"] == "root")

    result = _assert_ok(
        _call(
            "team_mission.node.start",
            {
                "mission_id": "mission-1",
                "node_id": node["node_id"],
                "use_strategy_prompt": True,
                "record_user_task_message": False,
            },
        )
    )

    row = _command_by_source(gateway_db, "mission:mission-1", "team_mission.node.start")
    assert row["kind"] == "start"
    assert row["payload"]["node_id"] == node["node_id"]
    assert row["payload"]["use_strategy_prompt"] is True
    assert result["mission_id"] == "mission-1"


def test_team_mission_cancel_records_activity_command(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    _create_mission(tmp_path)

    _assert_ok(
        _call(
            "team_mission.cancel",
            {
                "mission_id": "mission-1",
                "canceled_by": "user",
                "reason": "stop",
            },
        )
    )

    row = _command_by_source(gateway_db, "mission:mission-1", "team_mission.cancel")
    assert row["kind"] == "cancel"
    assert row["payload"] == {
        "mission_id": "mission-1",
        "canceled_by": "user",
        "reason": "stop",
    }


def test_team_mission_message_submit_records_activity_command(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    _assert_ok(
        _call(
            "team_mission.create",
            {
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
                "team_id": "team-1",
                "title": "Leader chat",
                "objective": "Talk",
                "mode": "supervised_mission",
                "conversation_only": True,
                "workspace": _workspace_payload(tmp_path),
                "metadata": {"stableTeamSessionId": "team-session-1"},
            },
        )
    )

    _assert_ok(
        _call(
            "team_mission.message.submit",
            {
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
                "team_id": "team-1",
                "text": "hello leader",
            },
        )
    )

    row = _command_by_source(
        gateway_db,
        "team-conversation:conversation-1",
        "team_mission.message.submit",
    )
    assert row["kind"] == "start"
    assert row["payload"]["conversation_session_id"] == "team-session-1"
    assert row["payload"]["text_len"] == len("hello leader")


def test_prompt_submit_records_activity_command_for_single_agent(
    monkeypatch: pytest.MonkeyPatch,
    gateway_db: SessionDB,
) -> None:
    from tui_gateway.methods import prompt as prompt_methods

    class NoStartThread:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        def start(self) -> None:
            return None

    gateway_db.create_session("prompt-session", source="tui")
    server._sessions["runtime-prompt"] = {
        "agent": object(),
        "session_key": "prompt-session",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transient": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
    }
    monkeypatch.setattr(prompt_methods.threading, "Thread", NoStartThread)
    monkeypatch.setattr(prompt_methods, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prompt_methods, "ensure_session_turn_toolsets", lambda **_kwargs: None)
    monkeypatch.setattr(prompt_methods, "_emit", lambda *_args, **_kwargs: None)

    result = _assert_ok(
        _call(
            "prompt.submit",
            {
                "_run_registry_reserved": True,
                "session_id": "runtime-prompt",
                "text": "hello",
                "client_run_id": "run-prompt",
                "turn_id": "turn-prompt",
            },
        )
    )

    row = _command_by_source(gateway_db, "chat:prompt-session", "prompt.submit")
    assert row["kind"] == "start"
    assert row["payload"] == {"session_id": "prompt-session", "text_len": 5}
    assert result["stored_session_id"] == "prompt-session"
    assert "command_id" not in result


def test_team_mission_start_task_tool_records_activity_command(
    monkeypatch: pytest.MonkeyPatch,
    gateway_db: SessionDB,
) -> None:
    from hermes_team_mission.tools import leader

    monkeypatch.setattr(leader, "_get_db", lambda _parent_agent=None: gateway_db)
    monkeypatch.setattr(
        leader,
        "_team_context",
        lambda: {
            "conversation_id": "conversation-tool",
            "conversation_session_id": "team-session-tool",
            "team_id": "team-1",
            "workspace_id": "workspace-tool",
            "workspace_path": "/tmp/workspace-tool",
            "mode": "supervised_mission",
            "members": [],
        },
    )
    monkeypatch.setattr(
        leader,
        "_gateway_call",
        lambda _method, _params: {
            "jsonrpc": "2.0",
            "id": "tool",
            "result": {"leader_start": {}, "graph": {"mission": {}, "nodes": []}},
        },
    )

    payload = json.loads(
        leader._handle_start_task(
            {
                "mission_id": "mission-tool",
                "task_id": "task-tool",
                "title": "Tool title",
                "objective": "Tool objective",
            }
        )
    )

    row = _command_by_source(
        gateway_db,
        "mission:mission-tool",
        "team_mission_start_task",
    )
    assert row["kind"] == "create"
    assert row["payload"]["task_id"] == "task-tool"
    assert payload["success"] is True
    assert payload["mission_id"] == "mission-tool"


def test_team_mission_create_response_shape_unchanged(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    result = _create_mission(tmp_path)

    assert set(result) == {"mission_id", "conversation_id", "graph"}
    assert "command_id" not in result


def test_team_mission_node_start_response_shape_unchanged(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    graph = _create_mission(tmp_path)
    node = next(item for item in graph["graph"]["nodes"] if item["kind"] == "root")

    result = _assert_ok(
        _call(
            "team_mission.node.start",
            {
                "mission_id": "mission-1",
                "node_id": node["node_id"],
                "record_user_task_message": False,
            },
        )
    )

    assert set(result) == {"mission_id", "node", "binding", "run", "stored_session_id"}
    assert "command_id" not in result


def test_team_mission_cancel_response_shape_unchanged(
    gateway_db: SessionDB,
    tmp_path: Path,
) -> None:
    _create_mission(tmp_path)

    result = _assert_ok(
        _call("team_mission.cancel", {"mission_id": "mission-1", "reason": "stop"})
    )

    assert set(result) == {
        "mission_id",
        "mission_status",
        "canceled_nodes",
        "canceled_runs",
        "cancel_errors",
        "graph",
    }
    assert "command_id" not in result


def test_legacy_bridge_with_no_db_does_not_crash_caller() -> None:
    assert (
        record_legacy_activity_command(
            None,
            activity_id="mission:mission-1",
            kind="create",
            source="team_mission.create",
        )
        == ""
    )


def test_legacy_bridge_failure_does_not_block_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    team_mission = team_mission_gateway()
    db = SessionDB(tmp_path / "state.db")
    _seed_registry_team(db, tmp_path)

    class InsertFailingDB:
        def __getattr__(self, name: str) -> Any:
            return getattr(db, name)

        def insert_activity_command(self, **_kwargs: Any) -> None:
            raise RuntimeError("activity command insert failed")

    monkeypatch.setattr(team_mission, "_get_db", lambda: InsertFailingDB())
    monkeypatch.setattr(server, "_db", db, raising=False)
    monkeypatch.setattr(server, "_db_error", None, raising=False)

    try:
        result = _assert_ok(
            _call(
                "team_mission.create",
                {
                    "mission_id": "mission-failure",
                    "conversation_id": "conversation-failure",
                    "conversation_session_id": "team-session-failure",
                    "team_id": "team-1",
                    "title": "Bridge failure",
                    "objective": "Legacy path should continue.",
                    "mode": "supervised_mission",
                    "workspace": _workspace_payload(tmp_path, "workspace-failure"),
                    "metadata": {"start_leader": False},
                    "record_user_task_message": False,
                },
            )
        )
    finally:
        db.close()

    assert result["mission_id"] == "mission-failure"
