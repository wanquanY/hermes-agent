from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_dispatch_lock import dispatch_tick_lock


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as connection:
        yield connection


def test_uncontended_dispatch_executes(conn):
    kb.create_task(conn, title="task", assignee="worker")

    result = kb.dispatch_once(conn, dry_run=True)

    assert result.skipped_locked is False
    assert result.dispatch_lock_error is None


def test_contended_board_skips_without_spawn_or_db_writes(conn):
    task = kb.create_task(conn, title="task", assignee="worker")
    spawn_calls: list[str] = []
    db_path = kb.kanban_db_path(board="default")

    with dispatch_tick_lock(db_path) as held:
        assert held.acquired is True
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda claimed, _workspace: spawn_calls.append(claimed.id),
        )

    assert result.skipped_locked is True
    assert result.dispatch_lock_error is None
    assert spawn_calls == []
    assert kb.get_task(conn, task).status == "ready"


def test_lock_is_board_scoped(kanban_home):
    first = kb.kanban_db_path(board="default")
    second = kanban_home / "kanban" / "boards" / "other" / "kanban.db"

    with dispatch_tick_lock(first) as first_lease:
        assert first_lease.acquired is True
        with dispatch_tick_lock(second) as second_lease:
            assert second_lease.acquired is True


def test_board_path_failure_is_explicit_and_fail_closed(conn, monkeypatch):
    spawn_calls: list[str] = []
    monkeypatch.setattr(
        kb,
        "kanban_db_path",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda claimed, _workspace: spawn_calls.append(claimed.id),
    )

    assert result.skipped_locked is True
    assert result.dispatch_lock_error.startswith("board_path_error:PermissionError")
    assert spawn_calls == []


def test_lock_open_failure_is_explicit_and_fail_closed(conn, monkeypatch):
    from hermes_agent.storage.process_lock import ProcessLockError
    from hermes_cli import kanban_dispatch_lock as lock_module

    class BrokenManager:
        def __enter__(self):
            raise ProcessLockError("read-only filesystem")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        lock_module,
        "exclusive_process_lock",
        lambda *_args, **_kwargs: BrokenManager(),
    )

    result = kb.dispatch_once(conn, dry_run=True)

    assert result.skipped_locked is True
    assert result.dispatch_lock_error.startswith("process_lock_error")
