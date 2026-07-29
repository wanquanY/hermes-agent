from __future__ import annotations

import threading
import time

import pytest

from hermes_agent.application.subagent_execution_service import (
    DaemonThreadPoolExecutor,
    ExecutionMode,
    ExecutionModeError,
    PersistenceRequiredError,
    SubagentExecutionRuntime,
    SubagentExecutionService,
    SubagentTaskSpec,
    resolve_execution_mode,
)
from hermes_agent.composition.cli_session_store import open_cli_session_store


def _service(
    tmp_path,
    ids=None,
    *,
    owner_pid=None,
    owner_run_id="",
    owner_turn_id="",
) -> SubagentExecutionService:
    values = iter(ids or [])
    return SubagentExecutionService(
        state_store=open_cli_session_store(tmp_path / "state.db"),
        conversation_session_id="conversation-parent",
        parent_activity_id="chat:conversation-parent",
        execution_scope_key="profile:parent",
        participant_id="participant-parent",
        profile_id="profile-parent",
        owner_pid=owner_pid,
        owner_run_id=owner_run_id,
        owner_turn_id=owner_turn_id,
        uuid_factory=lambda: next(values),
        time_fn=time.time,
    )


def _task(index: int, session: str) -> SubagentTaskSpec:
    return SubagentTaskSpec(
        task_index=index,
        goal=f"task {index}",
        child_session_id=session,
        subagent_id=f"sa-{index}",
        role="leaf",
        model="model",
        toolsets=("terminal",),
    )


@pytest.mark.parametrize(
    ("execution_mode", "background", "expected"),
    [
        (None, None, ExecutionMode.SYNC),
        ("sync", None, ExecutionMode.SYNC),
        ("async", None, ExecutionMode.ASYNC),
        (None, False, ExecutionMode.SYNC),
        (None, True, ExecutionMode.ASYNC),
        ("async", True, ExecutionMode.ASYNC),
    ],
)
def test_execution_mode_resolver(execution_mode, background, expected) -> None:
    assert resolve_execution_mode(
        execution_mode=execution_mode,
        background=background,
    ) is expected


def test_execution_mode_conflict_and_invalid_value_fail_closed() -> None:
    with pytest.raises(ExecutionModeError, match="conflicts"):
        resolve_execution_mode(execution_mode="sync", background=True)
    with pytest.raises(ExecutionModeError, match="sync.*async"):
        resolve_execution_mode(execution_mode="later")


def test_daemon_executor_has_no_hidden_queue_or_exit_blocking_worker() -> None:
    executor = DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="test-daemon")
    release = threading.Event()
    started = threading.Event()
    first = executor.submit(lambda: (started.set(), release.wait(2), "done")[2])
    assert started.wait(1)
    assert all(thread.daemon for thread in executor._threads)
    with pytest.raises(RuntimeError, match="capacity exceeded"):
        executor.submit(lambda: "queued")
    release.set()
    assert first.result(timeout=2) == "done"
    executor.shutdown(wait=True)


def test_sync_single_uses_one_activity_and_run_state_machine(tmp_path) -> None:
    service = _service(
        tmp_path,
        ids=["activity", "run", "turn"],
    )
    plan = service.create_plan([_task(0, "child-session")], mode=ExecutionMode.SYNC)
    service.start(plan)

    row = service._db.activities.get(plan.activity_id)
    assert row["status"] == "running"
    child = plan.children[0]
    assert service._db.runs.get(child.run_id)["status"] == "running"

    status = service.complete(
        plan,
        {"results": [{"task_index": 0, "status": "completed", "summary": "done"}]},
    )
    assert status == "completed"
    row = service._db.activities.get(plan.activity_id)
    assert row["status"] == "completed"
    assert row["result_summary"] == "done"
    assert service._db.runs.get(child.run_id)["status"] == "completed"
    events = service._db.runs.list_events_by_activity(plan.activity_id, include_internal=True)
    assert [event["payload"]["status"] for event in events] == ["running", "completed"]


def test_abnormal_interrupt_remains_distinct_from_user_cancellation(tmp_path) -> None:
    service = _service(tmp_path, ids=["activity", "run", "turn"])
    plan = service.create_plan(
        [_task(0, "child-session")],
        mode=ExecutionMode.SYNC,
    )
    service.start(plan)

    status = service.complete(
        plan,
        {
            "results": [
                {
                    "task_index": 0,
                    "status": "interrupted",
                    "error": "provider stream ended unexpectedly",
                }
            ]
        },
    )

    assert status == "interrupted"
    assert service._db.activities.get(plan.activity_id)["status"] == "interrupted"
    child = plan.children[0]
    assert service._db.runs.get(child.run_id)["status"] == "interrupted"
    events = service._db.runs.list_events_by_activity(
        plan.activity_id,
        include_internal=True,
    )
    assert events[-1]["payload"]["status"] == "interrupted"


def test_fanout_has_parent_and_ordered_child_activities(tmp_path) -> None:
    service = _service(
        tmp_path,
        ids=[
            "root",
            "child-0", "run-0", "turn-0",
            "child-1", "run-1", "turn-1",
            "child-2", "run-2", "turn-2",
        ],
    )
    plan = service.create_plan(
        [_task(index, f"session-{index}") for index in range(3)],
        mode=ExecutionMode.ASYNC,
    )
    service.start(plan)
    status = service.complete(
        plan,
        {
            "results": [
                {"task_index": 2, "status": "completed", "summary": "two"},
                {"task_index": 0, "status": "completed", "summary": "zero"},
                {"task_index": 1, "status": "error", "error": "one failed"},
            ]
        },
    )
    assert status == "failed"
    root = service._db.activities.get(plan.activity_id)
    assert root["status"] == "failed"
    assert [child.task_index for child in plan.children] == [0, 1, 2]
    rows = [service._db.activities.get(child.activity_id) for child in plan.children]
    assert [row["parent_activity_id"] for row in rows] == [plan.activity_id] * 3
    assert [row["status"] for row in rows] == ["completed", "failed", "completed"]


def test_async_requires_persistent_activity_store() -> None:
    service = SubagentExecutionService(
        state_store=None,
        conversation_session_id="conversation",
    )
    with pytest.raises(PersistenceRequiredError):
        service.create_plan([_task(0, "child")], mode=ExecutionMode.ASYNC)
    sync = service.create_plan([_task(0, "child")], mode=ExecutionMode.SYNC)
    assert sync.persistent is False


def test_async_runtime_returns_handle_and_persists_typed_terminal_event(tmp_path) -> None:
    service = _service(tmp_path, ids=["activity", "run", "turn"])
    plan = service.create_plan([_task(0, "child")], mode=ExecutionMode.ASYNC)
    runtime = SubagentExecutionRuntime()
    release = threading.Event()

    result = runtime.submit(
        plan=plan,
        service=service,
        runner=lambda: (
            release.wait(2),
            {"results": [{"task_index": 0, "status": "completed", "summary": "ok"}]},
        )[1],
        interrupt_fn=None,
        max_workers=1,
    )
    assert result["activity_id"] == plan.activity_id
    assert result["execution_mode"] == "async"
    assert runtime.active_count() == 1
    release.set()
    deadline = time.monotonic() + 2
    while runtime.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.active_count() == 0
    assert service._db.activities.get(plan.activity_id)["status"] == "completed"
    events = service._db.runs.list_events_by_activity(plan.activity_id, include_internal=True)
    assert events[-1]["type"] == "activity.state"
    assert events[-1]["internal"] is True


def test_runtime_cancels_only_detached_executions_owned_by_parent_run(tmp_path) -> None:
    owned_service = _service(
        tmp_path / "owned",
        ids=["activity-owned", "run-owned", "turn-owned"],
        owner_run_id="parent-run-1",
        owner_turn_id="parent-turn-1",
    )
    sibling_service = _service(
        tmp_path / "sibling",
        ids=["activity-sibling", "run-sibling", "turn-sibling"],
        owner_run_id="parent-run-2",
        owner_turn_id="parent-turn-2",
    )
    owned_plan = owned_service.create_plan(
        [_task(0, "owned-child")],
        mode=ExecutionMode.ASYNC,
    )
    sibling_plan = sibling_service.create_plan(
        [_task(0, "sibling-child")],
        mode=ExecutionMode.ASYNC,
    )
    runtime = SubagentExecutionRuntime()
    release = threading.Event()
    owned_interrupted = threading.Event()
    sibling_interrupted = threading.Event()

    for plan, service, interrupted in (
        (owned_plan, owned_service, owned_interrupted),
        (sibling_plan, sibling_service, sibling_interrupted),
    ):
        runtime.submit(
            plan=plan,
            service=service,
            runner=lambda: (
                release.wait(2),
                {"results": [{"task_index": 0, "status": "completed"}]},
            )[1],
            interrupt_fn=interrupted.set,
            max_workers=2,
        )

    assert runtime.cancel_owner_run(
        conversation_session_id="conversation-parent",
        owner_run_id="parent-run-1",
        owner_turn_id="parent-turn-1",
        reason="parent run cancelled",
    ) == 1
    assert owned_interrupted.is_set() is True
    assert sibling_interrupted.is_set() is False
    assert owned_service._db.activities.get(owned_plan.activity_id)["status"] == "cancelled"
    assert sibling_service._db.activities.get(sibling_plan.activity_id)["status"] == "running"

    release.set()


def test_owner_cancel_publishes_each_persisted_child_terminal_once(tmp_path) -> None:
    service = _service(
        tmp_path,
        ids=[
            "root",
            "child-0", "run-0", "turn-0",
            "child-1", "run-1", "turn-1",
        ],
        owner_run_id="parent-run",
        owner_turn_id="parent-turn",
    )
    plan = service.create_plan(
        [_task(0, "child-0"), _task(1, "child-1")],
        mode=ExecutionMode.ASYNC,
    )
    runtime = SubagentExecutionRuntime()
    release = threading.Event()
    published: list[tuple[int, str, str, list[str]]] = []

    runtime.submit(
        plan=plan,
        service=service,
        runner=lambda: (
            release.wait(2),
            {
                "results": [
                    {"task_index": 0, "status": "completed"},
                    {"task_index": 1, "status": "completed"},
                ]
            },
        )[1],
        interrupt_fn=None,
        max_workers=2,
        terminal_fn=lambda task_index, status, reason: published.append(
            (
                task_index,
                status,
                reason,
                [
                    service._db.activities.get(child.activity_id)["status"]
                    for child in plan.children
                ],
            )
        ),
    )

    assert runtime.cancel_owner_run(
        conversation_session_id="conversation-parent",
        owner_run_id="parent-run",
        owner_turn_id="parent-turn",
        reason="parent run cancelled",
    ) == 1
    assert [(index, status, reason) for index, status, reason, _ in published] == [
        (0, "cancelled", "parent run cancelled"),
        (1, "cancelled", "parent run cancelled"),
    ]
    assert all(statuses == ["cancelled", "cancelled"] for *_, statuses in published)

    # Duplicate cancel delivery is idempotent at both persistence and publish.
    assert runtime.cancel_owner_run(
        conversation_session_id="conversation-parent",
        owner_run_id="parent-run",
        owner_turn_id="parent-turn",
        reason="duplicate cancel",
    ) == 1
    assert len(published) == 2
    release.set()


def test_persisted_activity_cancel_interrupts_async_runner_and_wins_race(tmp_path) -> None:
    service = _service(tmp_path, ids=["activity", "run", "turn"])
    plan = service.create_plan([_task(0, "child")], mode=ExecutionMode.ASYNC)
    runtime = SubagentExecutionRuntime()
    interrupted = threading.Event()
    release = threading.Event()

    runtime.submit(
        plan=plan,
        service=service,
        runner=lambda: (
            release.wait(10),
            {"results": [{"task_index": 0, "status": "completed", "summary": "late"}]},
        )[1],
        interrupt_fn=lambda: interrupted.set(),
        max_workers=1,
    )
    deadline = time.monotonic() + 1
    while service._db.activities.get(plan.activity_id)["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    service._db.activities.mark_cancelled(plan.activity_id, result_summary="user stop")
    assert interrupted.wait(2)
    release.set()
    deadline = time.monotonic() + 2
    while runtime.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service._db.activities.get(plan.activity_id)["status"] == "cancelled"
    child = plan.children[0]
    assert service._db.runs.get(child.run_id)["status"] == "cancelled"
    events = service._db.runs.list_events_by_activity(
        child.activity_id,
        include_internal=True,
    )
    assert events[-1]["type"] == "activity.state"
    assert events[-1]["payload"]["status"] == "cancelled"


def test_runtime_capacity_rejects_without_implicit_sync_fallback(tmp_path) -> None:
    first = _service(tmp_path / "one", ids=["activity-1", "run-1", "turn-1"])
    second = _service(tmp_path / "two", ids=["activity-2", "run-2", "turn-2"])
    first_plan = first.create_plan([_task(0, "child-1")], mode=ExecutionMode.ASYNC)
    second_plan = second.create_plan([_task(0, "child-2")], mode=ExecutionMode.ASYNC)
    runtime = SubagentExecutionRuntime()
    release = threading.Event()
    runtime.submit(
        plan=first_plan,
        service=first,
        runner=lambda: (
            release.wait(2),
            {"results": [{"task_index": 0, "status": "completed"}]},
        )[1],
        interrupt_fn=None,
        max_workers=1,
    )
    rejected = runtime.submit(
        plan=second_plan,
        service=second,
        runner=lambda: {},
        interrupt_fn=None,
        max_workers=1,
    )
    assert rejected["status"] == "rejected"
    assert rejected["error_code"] == "capacity_exceeded"
    assert second._db.activities.get(second_plan.activity_id)["status"] == "pending"
    release.set()


def test_child_activity_cancel_interrupts_only_target_and_root_is_cancelled(
    tmp_path,
) -> None:
    service = _service(
        tmp_path,
        ids=[
            "root",
            "child-0", "run-0", "turn-0",
            "child-1", "run-1", "turn-1",
        ],
    )
    plan = service.create_plan(
        [_task(0, "child-0"), _task(1, "child-1")],
        mode=ExecutionMode.ASYNC,
    )
    runtime = SubagentExecutionRuntime()
    release = threading.Event()
    interrupted_children: list[int] = []
    interrupted_batch = threading.Event()
    runtime.submit(
        plan=plan,
        service=service,
        runner=lambda: (
            release.wait(2),
            {
                "results": [
                    {"task_index": 0, "status": "completed", "summary": "late"},
                    {"task_index": 1, "status": "completed", "summary": "ok"},
                ]
            },
        )[1],
        interrupt_fn=interrupted_batch.set,
        interrupt_child_fn=interrupted_children.append,
        max_workers=2,
    )
    target = plan.children[0]
    assert runtime.cancel(target.activity_id, reason="cancel one") is True
    assert interrupted_children == [0]
    assert interrupted_batch.is_set() is False
    assert service._db.activities.get(target.activity_id)["status"] == "cancelled"

    release.set()
    deadline = time.monotonic() + 2
    while runtime.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.active_count() == 0
    assert service._db.activities.get(plan.children[0].activity_id)["status"] == "cancelled"
    assert service._db.activities.get(plan.children[1].activity_id)["status"] == "completed"
    assert service._db.activities.get(plan.activity_id)["status"] == "cancelled"


def test_persisted_child_cancel_is_observed_without_stopping_sibling(tmp_path) -> None:
    service = _service(
        tmp_path,
        ids=[
            "root",
            "child-0", "run-0", "turn-0",
            "child-1", "run-1", "turn-1",
        ],
    )
    plan = service.create_plan(
        [_task(0, "child-0"), _task(1, "child-1")],
        mode=ExecutionMode.ASYNC,
    )
    runtime = SubagentExecutionRuntime()
    release = threading.Event()
    child_interrupted = threading.Event()
    batch_interrupted = threading.Event()
    runtime.submit(
        plan=plan,
        service=service,
        runner=lambda: (
            release.wait(10),
            {
                "results": [
                    {"task_index": 0, "status": "completed"},
                    {"task_index": 1, "status": "completed"},
                ]
            },
        )[1],
        interrupt_fn=batch_interrupted.set,
        interrupt_child_fn=lambda index: (
            child_interrupted.set() if index == 0 else None
        ),
        max_workers=2,
    )
    target = plan.children[0]
    deadline = time.monotonic() + 1
    while (
        service._db.activities.get(target.activity_id)["status"] != "running"
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert service._db.activities.mark_cancelled(
        target.activity_id,
        result_summary="stop child",
    )
    assert child_interrupted.wait(2)
    assert batch_interrupted.is_set() is False
    assert service._db.runs.get(target.run_id)["status"] == "cancelled"

    release.set()
    deadline = time.monotonic() + 2
    while runtime.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service._db.activities.get(plan.children[1].activity_id)["status"] == "completed"
    assert service._db.activities.get(plan.activity_id)["status"] == "cancelled"


def test_start_does_not_resurrect_pre_cancelled_activity(tmp_path) -> None:
    service = _service(tmp_path, ids=["activity", "run", "turn"])
    plan = service.create_plan([_task(0, "child")], mode=ExecutionMode.ASYNC)
    service.cancel(plan, reason="cancelled before scheduling")

    service.start(plan)

    assert service._db.activities.get(plan.activity_id)["status"] == "cancelled"
    run = service._db.runs.get(plan.children[0].run_id)
    assert run is not None
    assert run["status"] == "cancelled"


def test_orphan_recovery_fails_run_activity_and_emits_typed_event(tmp_path) -> None:
    service = _service(
        tmp_path,
        ids=["activity", "run", "turn"],
        owner_pid=999_999_999,
    )
    plan = service.create_plan([_task(0, "child")], mode=ExecutionMode.ASYNC)
    service.start(plan)

    recovered = service._db.runs.fail_orphaned(
        current_pid=1,
        stale_after_seconds=0,
        owner_dead_grace_seconds=0,
        reason="worker process disappeared",
    )

    assert recovered == 1
    child = plan.children[0]
    assert service._db.runs.get(child.run_id)["status"] == "failed"
    activity = service._db.activities.get(child.activity_id)
    assert activity["status"] == "failed"
    assert activity["result_summary"] == "worker process disappeared"
    events = service._db.runs.list_events_by_activity(
        child.activity_id,
        include_internal=True,
    )
    assert [event["payload"]["status"] for event in events] == [
        "running",
        "failed",
    ]


def test_orphan_recovery_fails_fanout_root(tmp_path) -> None:
    service = _service(
        tmp_path,
        ids=["root", "child-0", "run-0", "turn-0", "child-1", "run-1", "turn-1"],
        owner_pid=999_999_999,
    )
    plan = service.create_plan(
        [_task(0, "child-0"), _task(1, "child-1")],
        mode=ExecutionMode.ASYNC,
    )
    service.start(plan)

    assert service._db.runs.fail_orphaned(
        current_pid=1,
        stale_after_seconds=0,
        owner_dead_grace_seconds=0,
        reason="worker process disappeared",
    ) == 2
    assert service._db.activities.get(plan.activity_id)["status"] == "failed"
    assert [
        service._db.activities.get(child.activity_id)["status"]
        for child in plan.children
    ] == ["failed", "failed"]
