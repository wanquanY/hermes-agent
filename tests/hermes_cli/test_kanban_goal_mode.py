from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import goals
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_goal_mode_round_trips_and_defaults_off(kanban_home):
    with kb.connect() as conn:
        plain_id = kb.create_task(conn, title="plain", assignee="worker")
        goal_id = kb.create_task(
            conn,
            title="goal",
            assignee="worker",
            goal_mode=True,
            goal_max_turns=7,
        )
        plain = kb.get_task(conn, plain_id)
        goal = kb.get_task(conn, goal_id)
    assert plain.goal_mode is False
    assert plain.goal_max_turns is None
    assert goal.goal_mode is True
    assert goal.goal_max_turns == 7


def test_goal_columns_are_added_to_an_existing_board(kanban_home):
    db_path = kb.kanban_db_path()
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")

    with sqlite3.connect(db_path) as legacy:
        legacy.execute("ALTER TABLE tasks DROP COLUMN goal_mode")
        legacy.execute("ALTER TABLE tasks DROP COLUMN goal_max_turns")

    kb._INITIALIZED_PATHS.clear()
    with kb.connect(db_path) as migrated:
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        task = kb.get_task(migrated, task_id)

    assert {"goal_mode", "goal_max_turns"} <= columns
    assert task.goal_mode is False
    assert task.goal_max_turns is None


def test_spawn_enables_quiet_goal_worker_only_for_goal_cards(kanban_home, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="goal",
            assignee="worker",
            goal_mode=True,
            goal_max_turns=5,
        )
        task = kb.get_task(conn, task_id)
    kb._default_spawn(task, str(kanban_home))
    assert captured["env"]["HERMES_KANBAN_GOAL_MODE"] == "1"
    assert captured["env"]["HERMES_KANBAN_GOAL_MAX_TURNS"] == "5"
    assert "-Q" in captured["command"]


def test_spawn_leaves_plain_worker_environment_unchanged(kanban_home, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 4243

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="plain", assignee="worker")
        task = kb.get_task(conn, task_id)
    kb._default_spawn(task, str(kanban_home))
    assert "HERMES_KANBAN_GOAL_MODE" not in captured["env"]
    assert "HERMES_KANBAN_GOAL_MAX_TURNS" not in captured["env"]
    assert "-Q" not in captured["command"]


def test_goal_loop_stops_without_judging_when_worker_completed(monkeypatch):
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: pytest.fail("judge should not run"),
    )
    turns = []
    result = goals.run_kanban_goal_loop(
        task_id="task-complete",
        goal_text="ship it",
        run_turn=lambda prompt: turns.append(prompt) or "unexpected",
        task_status_fn=lambda: "done",
        block_fn=lambda _reason: pytest.fail("should not block"),
        first_response="already done",
    )
    assert result["outcome"] == "completed_by_worker"
    assert turns == []


def test_goal_loop_continues_in_session_until_worker_completes(monkeypatch):
    verdicts = iter(["continue", "continue"])
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: (next(verdicts), "more work", False, None),
    )
    statuses = iter(["running", "running", "done"])
    turns = []
    result = goals.run_kanban_goal_loop(
        task_id="task-1",
        goal_text="ship it",
        run_turn=lambda prompt: turns.append(prompt) or "progress",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _reason: pytest.fail("should not block"),
        max_turns=5,
        first_response="started",
    )
    assert result["outcome"] == "completed_by_worker"
    assert len(turns) == 2


def test_goal_loop_blocks_instead_of_silently_exiting_on_budget(monkeypatch):
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "unfinished", False, None),
    )
    blocked = []
    result = goals.run_kanban_goal_loop(
        task_id="task-2",
        goal_text="finish",
        run_turn=lambda _prompt: "still working",
        task_status_fn=lambda: "running",
        block_fn=blocked.append,
        max_turns=2,
        first_response="started",
    )
    assert result["outcome"] == "blocked_budget"
    assert blocked and "turn budget" in blocked[0]


def test_goal_loop_nudges_worker_to_finalize_judge_complete_work(monkeypatch):
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("done", "verified", False, None),
    )
    statuses = iter(["running", "done"])
    turns = []
    result = goals.run_kanban_goal_loop(
        task_id="task-finalize",
        goal_text="ship it",
        run_turn=lambda prompt: turns.append(prompt) or "finalized",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _reason: pytest.fail("should not block"),
        first_response="work is complete",
    )
    assert result["outcome"] == "completed_by_worker"
    assert len(turns) == 1
    assert "still open" in turns[0]


def test_goal_loop_blocks_when_worker_ignores_finalize_nudge(monkeypatch):
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("done", "verified", False, None),
    )
    blocked = []
    result = goals.run_kanban_goal_loop(
        task_id="task-open",
        goal_text="ship it",
        run_turn=lambda _prompt: "still open",
        task_status_fn=lambda: "running",
        block_fn=blocked.append,
        first_response="work is complete",
    )
    assert result["outcome"] == "blocked_budget"
    assert blocked and "finalize" in blocked[0].lower()


def test_goal_loop_stops_if_task_is_no_longer_owned(monkeypatch):
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "more", False, None),
    )
    result = goals.run_kanban_goal_loop(
        task_id="task-reclaimed",
        goal_text="ship it",
        run_turn=lambda _prompt: pytest.fail("should not run another turn"),
        task_status_fn=lambda: "archived",
        block_fn=lambda _reason: pytest.fail("should not block"),
        first_response="partial",
    )
    assert result["outcome"] == "stopped"
