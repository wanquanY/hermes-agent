from pathlib import Path

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_project_lifecycle_and_longest_folder_ownership(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    outer = store.projects.create(name="Outer", folders=[str(tmp_path)])
    inner_path = tmp_path / "app"
    inner_path.mkdir()
    inner = store.projects.create(name="Inner", folders=[str(inner_path)])

    assert outer.slug == "outer"
    assert inner.primary_path == str(inner_path.resolve())
    assert store.projects.project_for_path(str(inner_path / "src")).id == inner.id

    extra = tmp_path / "other"
    extra.mkdir()
    inner = store.projects.add_folder(inner.id, str(extra), is_primary=True)
    assert inner.primary_path == str(extra.resolve())
    assert sum(folder.is_primary for folder in inner.folders) == 1

    inner = store.projects.remove_folder(inner.id, str(extra))
    assert inner.primary_path == str(inner_path.resolve())
    store.close()


def test_project_slugs_active_pointer_archive_and_discovery(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    first = store.projects.create(name="My Project")
    second = store.projects.create(name="My Project")
    assert (first.slug, second.slug) == ("my-project", "my-project-2")

    assert store.projects.set_active(first.slug) == first.id
    assert store.projects.active_id() == first.id
    archived = store.projects.set_archived(first.id, True)
    assert archived.archived is True
    assert [project.id for project in store.projects.list()] == [second.id]

    count = store.projects.record_discovered_repos(
        [(str(tmp_path / "repo"), "repo")],
        replace=True,
    )
    assert count == 1
    assert store.projects.list_discovered_repos()[0]["label"] == "repo"
    store.close()


def test_project_validation_and_branch_naming(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    with pytest.raises(ValueError, match="name"):
        store.projects.create(name="")
    with pytest.raises(ValueError, match="slug"):
        store.projects.create(name="Bad", slug="../bad")

    project = store.projects.create(name="Hermes Agent", folders=[str(tmp_path)])
    assert project.branch_name("t_123", title="Fix Login!") == (
        "hermes-agent/t_123-fix-login"
    )
    store.close()


def test_session_git_context_and_project_discovery_projection(tmp_path: Path) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.sessions.create("s1", "tui", cwd=str(repo))
    store.messages.append("s1", "user", "hello")

    assert store.sessions.update_git_context(
        "s1",
        branch="feature/projects",
        repo_root=str(repo),
    )
    session = store.sessions.get("s1")
    assert session["git_branch"] == "feature/projects"
    assert session["git_repo_root"] == str(repo)
    assert store.sessions.distinct_cwds() == [
        {"cwd": str(repo), "sessions": 1, "last_active": session["last_active"]}
    ]

    assert store.sessions.backfill_repo_roots({str(repo): str(tmp_path)}) == 1
    assert store.sessions.get("s1")["git_repo_root"] == str(tmp_path)
    store.close()
