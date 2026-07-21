from __future__ import annotations

import json

from hermes_agent.composition import cli_session_store
from tools import project_tools


def _bind_store(monkeypatch, tmp_path):
    db_path = tmp_path / "sessions.db"
    original = cli_session_store.open_cli_session_store
    monkeypatch.setattr(
        cli_session_store,
        "open_cli_session_store",
        lambda: original(db_path),
    )
    return db_path


def test_project_tools_use_canonical_store_and_move_workspace(monkeypatch, tmp_path):
    _bind_store(monkeypatch, tmp_path)
    folder = tmp_path / "workspace"
    folder.mkdir()
    moves = []
    project_tools.set_project_workspace_callback(
        lambda task_id, path, name: moves.append((task_id, path, name))
    )

    created = json.loads(
        project_tools.project_create("Aurora", str(folder), task_id="session-1")
    )
    assert created["success"] is True
    assert created["primary_path"] == str(folder)
    assert moves == [("session-1", str(folder), "Aurora")]

    listing = json.loads(project_tools.project_list())
    assert listing["active_id"] == created["id"]
    assert listing["projects"][0]["slug"] == "aurora"

    switched = json.loads(
        project_tools.project_switch("AURORA", task_id="session-2")
    )
    assert switched["id"] == created["id"]
    assert moves[-1] == ("session-2", str(folder), "Aurora")


def test_project_create_validates_name(monkeypatch, tmp_path):
    _bind_store(monkeypatch, tmp_path)
    result = json.loads(project_tools.project_create("   "))
    assert result == {"success": False, "error": "name is required"}


def test_project_toolset_contains_only_intentional_project_tools():
    from toolsets import resolve_toolset

    assert set(resolve_toolset("project")) == {
        "project_list",
        "project_create",
        "project_switch",
    }
