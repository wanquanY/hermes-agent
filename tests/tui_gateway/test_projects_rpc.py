from __future__ import annotations

import subprocess

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway import git_probe, server
from tui_gateway.methods import projects


@pytest.fixture
def project_store(monkeypatch, tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(projects, "_store", lambda: store)
    yield store
    store.close()


def _call(method: str, params: dict | None = None) -> dict:
    response = server._methods[method]("rpc", params or {})
    assert "error" not in response, response.get("error")
    return response["result"]


def test_project_methods_are_registered_and_slow_probes_leave_dispatch_thread():
    for name in (
        "projects.list",
        "projects.get",
        "projects.create",
        "projects.update",
        "projects.add_folder",
        "projects.remove_folder",
        "projects.set_primary",
        "projects.archive",
        "projects.delete",
        "projects.set_active",
        "projects.for_cwd",
        "projects.discover_repos",
        "projects.record_repos",
        "projects.tree",
        "projects.project_sessions",
    ):
        assert name in server._methods
    for name in (
        "projects.for_cwd",
        "projects.discover_repos",
        "projects.record_repos",
        "projects.tree",
        "projects.project_sessions",
    ):
        assert name in server._LONG_HANDLERS


def test_project_crud_active_and_longest_folder_owner(project_store, tmp_path):
    outer = _call(
        "projects.create",
        {"name": "Outer", "folders": [str(tmp_path)], "use": True},
    )["project"]
    inner = tmp_path / "inner"
    inner.mkdir()
    created = _call(
        "projects.create",
        {"name": "Inner", "folders": [str(inner)]},
    )["project"]

    listing = _call("projects.list")
    assert listing["active_id"] == outer["id"]
    assert {item["slug"] for item in listing["projects"]} == {"outer", "inner"}

    nested = inner / "src"
    nested.mkdir()
    resolved = _call("projects.for_cwd", {"cwd": str(nested)})
    assert resolved["project"]["id"] == created["id"]
    assert "branch" in resolved

    updated = _call(
        "projects.update",
        {"id": created["id"], "name": "Renamed"},
    )
    assert updated["project"]["name"] == "Renamed"


def test_project_mutations_refresh_live_session_status_projection(
    project_store,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with server._sessions_lock:
        server._sessions["live-project"] = {
            "cwd": str(workspace),
            "session_key": "stored-project",
        }
    try:
        project = _call(
            "projects.create",
            {"name": "Workspace", "folders": [str(workspace)]},
        )["project"]
        assert server._sessions["live-project"]["project"] == {
            "id": project["id"],
            "slug": "workspace",
            "name": "Workspace",
            "primary_path": str(workspace),
        }

        _call("projects.archive", {"id": project["id"]})
        assert server._sessions["live-project"]["project"] is None
    finally:
        with server._sessions_lock:
            server._sessions.pop("live-project", None)


def test_discover_repos_backfills_session_git_root(project_store, tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "src"
    nested.mkdir(parents=True)
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    project_store.sessions.create("s1", "tui", cwd=str(nested))
    project_store.messages.append("s1", "user", "hello")
    git_probe.invalidate()

    discovered = _call("projects.discover_repos")["repos"]
    assert discovered[0]["root"] == str(repo)
    assert discovered[0]["sessions"] == 1
    assert project_store.sessions.get("s1")["git_repo_root"] == str(repo)


def test_recorded_zero_session_repo_and_project_tree(project_store, tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    recorded = _call(
        "projects.record_repos",
        {"repos": [{"root": str(repo), "label": "fresh"}]},
    )
    assert recorded["repos"][0]["label"] == "fresh"
    assert recorded["repos"][0]["sessions"] == 0

    project = _call(
        "projects.create",
        {"name": "Fresh", "folders": [str(repo)]},
    )["project"]
    project_store.sessions.create("s1", "tui", cwd=str(repo))
    project_store.messages.append("s1", "user", "hello")
    tree = _call("projects.tree")
    node = next(item for item in tree["projects"] if item["id"] == project["id"])
    assert node["sessionCount"] == 1
    hydrated = _call(
        "projects.project_sessions",
        {"project_id": project["id"]},
    )["project"]
    sessions = [
        session
        for repo_node in hydrated["repos"]
        for lane in repo_node["groups"]
        for session in lane["sessions"]
    ]
    assert [session["id"] for session in sessions] == ["s1"]


def test_unknown_project_maps_to_typed_error(project_store):
    response = server._methods["projects.get"]("rpc", {"id": "missing"})
    assert response["error"]["code"] == 5062
