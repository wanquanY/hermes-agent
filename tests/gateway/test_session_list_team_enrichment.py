from __future__ import annotations

import importlib
from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway import server
from tui_gateway.services.workspace import bind_session_workspace


def _setup(monkeypatch, tmp_path: Path) -> CliSessionStore:
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(session_methods, "_get_db", lambda: db)
    return db


def _session_list() -> list[dict]:
    response = server._methods["session.list"](1, {})
    assert "error" not in response
    return response["result"]["sessions"]


def _session_index_list() -> list[dict]:
    response = server._methods["session.index.list"](1, {})
    assert "error" not in response
    return response["result"]["sessions"]


def _create_team_sidebar_session(
    db: CliSessionStore,
    *,
    conversation_id: str = "conversation-1",
    conversation_session_id: str = "team-session-1",
    team_id: str = "team-1",
    title: str = "Team Conversation",
    active_mission_id: str = "mission-1",
) -> None:
    db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        team_id=team_id,
        title=title,
        objective="Team objective",
        active_mission_id=active_mission_id,
        created_at=100,
        updated_at=200,
    )
    db.sessions.create(conversation_session_id, source="team_mission", transient=False)
    db.messages.append(conversation_session_id, role="user", content="hello team")


def test_session_list_returns_team_metadata_for_team_session(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(
        db,
        title="Backend Team Title",
        active_mission_id="mission-active",
    )

    [item] = _session_list()

    assert item["id"] == "team-session-1"
    assert item["session_kind"] == "team_mission"
    assert item["conversation_id"] == "conversation-1"
    assert item["team_id"] == "team-1"
    assert item["team_conversation_title"] == "Backend Team Title"
    assert item["mission_status"] == "active"
    assert item["running"] is True


def test_session_list_returns_null_team_fields_for_direct_session(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    db.sessions.create("direct-session-1", source="cli", transient=False)
    db.messages.append("direct-session-1", role="user", content="hello")

    [item] = _session_list()

    assert item["id"] == "direct-session-1"
    assert item["team_id"] == ""
    assert item["team_conversation_title"] == ""
    assert item["mission_status"] == ""
    assert item["active_mission_id"] == ""
    assert item["running"] is False


def test_session_list_running_reflects_any_active_mission(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(db, active_mission_id="")
    db.add_mission_to_conversation(
        conversation_id="conversation-1",
        mission_id="mission-latest-active",
    )

    [item] = _session_list()

    assert db.has_active_mission("conversation-1") is True
    assert item["running"] is True
    assert item["active_mission_id"] == "mission-latest-active"
    assert item["mission_status"] == "active"


def test_session_list_includes_active_mission_id_when_set(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(db, active_mission_id="mission-explicit")

    [item] = _session_list()

    assert item["active_mission_id"] == "mission-explicit"
    assert item["mission_id"] == "mission-explicit"


def test_session_index_list_also_enriched_for_consistency(monkeypatch, tmp_path: Path):
    db = _setup(monkeypatch, tmp_path)
    _create_team_sidebar_session(
        db,
        title="Indexed Team Title",
        active_mission_id="mission-index",
    )
    db.session_index.upsert(
        session_id="direct-session-1",
        title="Direct",
        preview="hello",
        source="cli",
        started_at=300,
        updated_at=300,
    )

    by_id = {item["id"]: item for item in _session_index_list()}
    team_item = by_id["team-session-1"]
    direct_item = by_id["direct-session-1"]

    assert team_item["team_id"] == "team-1"
    assert team_item["team_conversation_title"] == "Indexed Team Title"
    assert team_item["active_mission_id"] == "mission-index"
    assert team_item["mission_status"] == "active"
    assert team_item["running"] is True
    assert direct_item["team_id"] == ""
    assert direct_item["team_conversation_title"] == ""
    assert direct_item["mission_status"] == ""


def test_session_index_list_embeds_workspace_team_context_and_derived_state(
    monkeypatch,
    tmp_path: Path,
):
    db = _setup(monkeypatch, tmp_path)
    workspace_path = str(tmp_path)
    db.upsert_team_mission(
        mission_id="mission-index-contract",
        conversation_id="conversation-index-contract",
        team_id="team-index-contract",
        title="Mission",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_conversation(
        conversation_id="conversation-index-contract",
        conversation_session_id="team-session-index-contract",
        team_id="team-index-contract",
        title="Indexed Team",
        active_mission_id="mission-index-contract",
        workspace_id="workspace-index-contract",
        workspace_path=workspace_path,
        created_at=100,
        updated_at=200,
    )
    db.upsert_team_mission_node(
        mission_id="mission-index-contract",
        node_id="approval",
        kind="approval_gate",
        title="Approve",
        status="waiting_approval",
    )
    db.session_index.upsert(
        session_id="plain-session-index-contract",
        title="Plain",
        source="cli",
        conversation_kind="direct",
        started_at=50,
        updated_at=50,
    )
    bind_session_workspace(
        session_id="team-session-index-contract",
        cwd=workspace_path,
        workspace={
            "id": "workspace-index-contract",
            "name": "Workspace",
            "path": workspace_path,
            "kind": "local",
        },
    )

    by_id = {item["id"]: item for item in _session_index_list()}
    team_item = by_id["team-session-index-contract"]
    plain_item = by_id["plain-session-index-contract"]

    assert team_item["workspace_binding"] == {
        "workspace_id": "workspace-index-contract",
        "workspace_path": workspace_path,
    }
    assert team_item["team_context"] == {
        "team_id": "team-index-contract",
        "team_conversation_id": "conversation-index-contract",
        "mission_id": "mission-index-contract",
        "member_id": "",
    }
    assert team_item["derived_state"] == {
        "running": False,
        "waiting_approval": True,
        "terminal_status": None,
    }
    assert plain_item["workspace_binding"] is None
    assert plain_item["team_context"] is None
    assert plain_item["derived_state"] == {
        "running": False,
        "waiting_approval": False,
        "terminal_status": None,
    }
