from __future__ import annotations

import argparse

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_cli import projects_cmd


@pytest.fixture
def project_db(monkeypatch, tmp_path):
    path = tmp_path / "state.db"
    monkeypatch.setattr(
        projects_cmd,
        "open_cli_session_store",
        lambda: open_cli_session_store(path),
    )
    return path


def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    project = projects_cmd.build_parser(subparsers)
    project.set_defaults(func=projects_cmd.projects_command)
    return projects_cmd.projects_command(parser.parse_args(["project", *argv]))


def test_create_list_show_and_use(project_db, capsys, tmp_path):
    assert _run(["create", "My App", str(tmp_path), "--use"]) == 0
    assert "Created project" in capsys.readouterr().out
    assert _run(["list"]) == 0
    assert "my-app" in capsys.readouterr().out
    assert _run(["show", "my-app"]) == 0
    assert "My App" in capsys.readouterr().out

    store = open_cli_session_store(project_db)
    project = store.projects.get("my-app")
    assert store.projects.active_id() == project.id
    store.close()


def test_folder_rename_archive_restore_and_clear(project_db, capsys, tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    assert _run(["create", "Old", str(first)]) == 0
    assert _run(["add-folder", "old", str(second), "--primary"]) == 0
    assert _run(["rename", "old", "New Name"]) == 0
    assert _run(["archive", "old"]) == 0
    assert _run(["restore", "old"]) == 0
    assert _run(["use", "old"]) == 0
    assert _run(["use"]) == 0
    capsys.readouterr()

    store = open_cli_session_store(project_db)
    project = store.projects.get("old")
    assert project.name == "New Name"
    assert project.primary_path == str(second)
    assert project.archived is False
    assert store.projects.active_id() is None
    store.close()


def test_unknown_project_returns_error(project_db, capsys):
    assert _run(["show", "missing"]) == 1
    assert "no such project" in capsys.readouterr().err
