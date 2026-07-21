"""Integration tests for Activity-backed asynchronous delegation.

hermes-test-runner: serial

The first test enforces a wall-clock return-time contract.  It must measure
the delegation path itself, not CPU contention from unrelated pytest
processes in the per-file parallel runner.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_team_mission.domain.run_context import RunContext
from tools import async_delegation as legacy_async
from tools.process_registry import format_process_notification, process_registry


@pytest.fixture(autouse=True)
def _clean_runtime_and_process_queue():
    legacy_async._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    legacy_async._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def _parent(tmp_path):
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "conversation-parent"
    parent.gateway_session_key = ""
    parent.runtime_scope_key = "conversation-parent"
    parent._session_db = open_cli_session_store(tmp_path / "state.db")
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._memory_manager = None
    parent._delegation_result_lock = threading.RLock()
    return parent


def _child(index: int):
    child = MagicMock()
    child.session_id = f"child-session-{index}"
    child._delegate_role = "leaf"
    child._subagent_id = f"sa-{index}"
    child._subagent_toolsets = ["terminal"]
    child.model = "test-model"
    return child


def _credentials():
    return {
        "model": "test-model",
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "command": None,
        "args": None,
    }


def test_delegate_task_async_returns_persistent_handle_without_blocking(
    monkeypatch,
    tmp_path,
) -> None:
    import tools.delegate_tool as delegate_tool

    parent = _parent(tmp_path)
    child = _child(0)
    release = threading.Event()

    def build(**_kwargs):
        parent._active_children.append(child)
        return child

    def run(task_index, goal, _child_arg=None, _parent_arg=None, **_kwargs):
        release.wait(3)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": f"done: {goal}",
            "api_calls": 1,
            "duration_seconds": 0.1,
        }

    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)
    monkeypatch.setattr(delegate_tool, "_run_single_child", run)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _credentials(),
    )

    started = time.monotonic()
    result = json.loads(
        delegate_tool.delegate_task(
            goal="inspect repository",
            execution_mode="async",
            parent_agent=parent,
        )
    )
    assert time.monotonic() - started < 1.0
    assert result["status"] == "running"
    assert result["execution_mode"] == "async"
    assert result["activity_id"].startswith("act-agent_dispatch:")
    assert result["child_activity_ids"] == [result["activity_id"]]
    assert len(result["live_transcripts"]) == 1
    live_transcript = result["live_transcripts"][0]
    assert live_transcript.endswith("task-0.log")
    assert os.path.exists(live_transcript)
    assert child not in parent._active_children
    assert legacy_async.active_count() == 1
    assert process_registry.completion_queue.empty()

    release.set()
    _wait_until(lambda: legacy_async.active_count() == 0)
    activity = parent._session_db.activities.get(result["activity_id"])
    assert activity["status"] == "completed"
    assert activity["result_summary"] == "done: inspect repository"
    transcript_text = Path(live_transcript).read_text(encoding="utf-8")
    assert "end status=completed" in transcript_text
    events = parent._session_db.runs.list_events_by_activity(
        result["activity_id"],
        include_internal=True,
    )
    assert [event["type"] for event in events] == [
        "activity.state",
        "activity.state",
    ]
    assert process_registry.completion_queue.empty()


def test_async_fanout_has_parent_and_ordered_children(monkeypatch, tmp_path) -> None:
    import tools.delegate_tool as delegate_tool

    parent = _parent(tmp_path)
    children = [_child(index) for index in range(3)]
    release = threading.Event()

    def build(task_index, **_kwargs):
        child = children[task_index]
        parent._active_children.append(child)
        return child

    def run(task_index, goal, **_kwargs):
        release.wait(3)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": f"{task_index}:{goal}",
            "duration_seconds": 0.1,
        }

    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)
    monkeypatch.setattr(delegate_tool, "_run_single_child", run)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _credentials(),
    )
    result = json.loads(
        delegate_tool.delegate_task(
            tasks=[{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
            background=True,
            parent_agent=parent,
        )
    )
    assert result["execution_mode"] == "async"
    assert len(result["child_activity_ids"]) == 3
    assert len(set(result["child_activity_ids"])) == 3
    _wait_until(
        lambda: all(
            parent._session_db.activities.get(activity_id) is not None
            for activity_id in result["child_activity_ids"]
        )
    )
    for activity_id in result["child_activity_ids"]:
        row = parent._session_db.activities.get(activity_id)
        assert row["parent_activity_id"] == result["activity_id"]

    release.set()
    _wait_until(lambda: legacy_async.active_count() == 0)
    root = parent._session_db.activities.get(result["activity_id"])
    assert root["status"] == "completed"
    assert [
        parent._session_db.activities.get(activity_id)["result_summary"]
        for activity_id in result["child_activity_ids"]
    ] == ["0:a", "1:b", "2:c"]


def test_async_child_inherits_identity_with_own_activity_and_execution_session(
    monkeypatch,
    tmp_path,
) -> None:
    import tools.delegate_tool as delegate_tool

    parent = _parent(tmp_path)
    parent.run_context = RunContext(
        conversation_session_id="conversation-parent",
        participant_id="participant-parent",
        activity_id="chat:conversation-parent",
        activity_kind="chat",
        execution_scope_key="profile:parent",
        control_home=str(tmp_path / "control"),
        execution_home=str(tmp_path / "execution"),
        profile_id="profile-parent",
        profile_version_id="profile-version-parent",
    )
    child = _child(0)
    release = threading.Event()

    def build(**_kwargs):
        parent._active_children.append(child)
        return child

    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda task_index, _goal, _child_arg, _parent_arg, **_kwargs: (
            release.wait(3),
            {"task_index": task_index, "status": "completed", "summary": "ok"},
        )[1],
    )
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _credentials(),
    )
    result = json.loads(
        delegate_tool.delegate_task(
            goal="identity",
            execution_mode="async",
            parent_agent=parent,
        )
    )
    assert child.run_context.conversation_session_id == "conversation-parent"
    assert child.run_context.participant_id == "participant-parent"
    assert child.run_context.profile_id == "profile-parent"
    assert child.run_context.activity_kind == "agent_dispatch"
    assert child.run_context.activity_id == result["activity_id"]
    assert child.run_context.execution_session_id == "child-session-0"
    assert child.run_context.execution_scope_key == "profile:parent"
    release.set()
    _wait_until(lambda: legacy_async.active_count() == 0)


def test_parent_turn_interrupt_does_not_cancel_detached_async_child(
    monkeypatch,
    tmp_path,
) -> None:
    import tools.delegate_tool as delegate_tool

    parent = _parent(tmp_path)
    child = _child(0)
    release = threading.Event()

    def build(**_kwargs):
        parent._active_children.append(child)
        return child

    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda task_index, _goal, _child_arg, _parent_arg, **_kwargs: (
            release.wait(3),
            {
                "task_index": task_index,
                "status": "completed",
                "summary": "survived",
            },
        )[1],
    )
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _credentials(),
    )
    result = json.loads(
        delegate_tool.delegate_task(
            goal="detached",
            execution_mode="async",
            parent_agent=parent,
        )
    )
    parent._interrupt_requested = True
    time.sleep(0.05)
    assert parent._session_db.activities.get(result["activity_id"])["status"] == "running"
    release.set()
    _wait_until(lambda: legacy_async.active_count() == 0)
    assert parent._session_db.activities.get(result["activity_id"])["status"] == "completed"


def test_explicit_activity_cancel_interrupts_async_child(monkeypatch, tmp_path) -> None:
    import tools.delegate_tool as delegate_tool

    parent = _parent(tmp_path)
    child = _child(0)
    release = threading.Event()

    def build(**_kwargs):
        parent._active_children.append(child)
        return child

    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)
    monkeypatch.setattr(
        delegate_tool,
        "_run_single_child",
        lambda task_index, _goal, _child_arg, _parent_arg, **_kwargs: (
            release.wait(3),
            {
                "task_index": task_index,
                "status": "completed",
                "summary": "late",
            },
        )[1],
    )
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _credentials(),
    )
    result = json.loads(
        delegate_tool.delegate_task(
            goal="cancel me",
            execution_mode="async",
            parent_agent=parent,
        )
    )
    assert legacy_async.cancel_async_delegation(
        result["activity_id"],
        reason="user stop",
    )
    assert child.interrupt.called
    assert parent._session_db.activities.get(result["activity_id"])["status"] == "cancelled"
    release.set()
    _wait_until(lambda: legacy_async.active_count() == 0)
    assert parent._session_db.activities.get(result["activity_id"])["status"] == "cancelled"


def test_async_requires_persistent_activity_store(monkeypatch) -> None:
    import tools.delegate_tool as delegate_tool

    agent = MagicMock()
    agent._delegate_depth = 0
    agent.session_id = "conversation"
    agent._session_db = None
    agent._active_children = []
    agent._active_children_lock = None
    child = _child(0)
    monkeypatch.setattr(delegate_tool, "_build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: _credentials(),
    )
    result = json.loads(
        delegate_tool.delegate_task(
            goal="cannot detach",
            execution_mode="async",
            parent_agent=agent,
        )
    )
    assert "error" in result
    assert "requires an Activity/Run state store" in result["error"]


def test_nested_orchestrator_cannot_create_detached_daemon_tree() -> None:
    from tools.delegate_tool import delegate_task

    parent = MagicMock()
    parent._delegate_depth = 1
    result = json.loads(
        delegate_task(
            goal="nested async",
            execution_mode="async",
            parent_agent=parent,
        )
    )
    assert "error" in result
    assert "Nested async delegation is not allowed" in result["error"]


def test_execution_choice_is_forwarded_by_main_agent() -> None:
    import run_agent

    class FakeAgent:
        _delegate_depth = 0

    captured = {}

    def fake_delegate(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", fake_delegate):
        run_agent.AIAgent._dispatch_delegate_task(
            FakeAgent(),
            {"goal": "sync", "execution_mode": "sync"},
        )
        assert captured["execution_mode"] == "sync"
        run_agent.AIAgent._dispatch_delegate_task(
            FakeAgent(),
            {"goal": "async", "execution_mode": "async", "background": True},
        )
        assert captured["execution_mode"] == "async"
        assert captured["background"] is True


def test_legacy_dispatcher_is_inert_and_activity_events_never_form_prompts() -> None:
    result = legacy_async.dispatch_async_delegation(runner=lambda: {})
    assert result["status"] == "rejected"
    assert result["error_code"] == "retired_api"
    assert legacy_async.active_count() == 0
    assert format_process_notification(
        {
            "type": "activity.state",
            "session_key": "conversation",
            "activity_id": "act-agent_dispatch:one",
            "status": "completed",
        }
    ) is None
    assert process_registry.completion_queue.empty()
