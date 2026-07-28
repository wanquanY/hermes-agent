from __future__ import annotations

import asyncio

import pytest

from hermes_agent.application.active_work_registry import (
    ActiveWorkRegistry,
    ActiveWorkState,
    WorkRejected,
)


def test_begin_drain_linearizes_with_new_work_admission():
    registry = ActiveWorkRegistry()
    lease = registry.register(kind="gateway_turn", surface="gateway", work_id="turn-1")

    active = registry.begin_drain()

    assert [item.work_id for item in active] == ["turn-1"]
    with pytest.raises(WorkRejected) as rejected:
        registry.register(kind="api_run", surface="api", work_id="run-2")
    assert rejected.value.code == "runtime_draining"
    lease.release()


@pytest.mark.asyncio
async def test_clean_drain_waits_for_existing_work_and_stops():
    registry = ActiveWorkRegistry()
    lease = registry.register(kind="api_run", surface="api", work_id="run-1")
    asyncio.get_running_loop().call_later(0.01, lease.release)

    report = await registry.drain(timeout=1.0, cancel_grace=0.1)

    assert report.clean is True
    assert report.deadline_expired == ()
    assert report.timed_out == ()
    assert registry.state is ActiveWorkState.STOPPED


@pytest.mark.asyncio
async def test_timeout_persists_before_cancel_and_isolates_callback_errors():
    registry = ActiveWorkRegistry()
    order: list[str] = []
    lease_a = registry.register(
        kind="cron",
        surface="scheduler",
        work_id="job-a",
        persist_timeout=lambda: order.append("persist-a"),
    )
    lease_b = registry.register(
        kind="api_run",
        surface="api",
        work_id="run-b",
        persist_timeout=lambda: (_ for _ in ()).throw(RuntimeError("store down")),
    )

    def cancel_a() -> None:
        order.append("cancel-a")
        lease_a.release()

    def cancel_b() -> None:
        order.append("cancel-b")
        lease_b.release()

    lease_a.set_callbacks(persist_timeout=lambda: order.append("persist-a"), cancel=cancel_a)
    lease_b.set_callbacks(
        persist_timeout=lambda: (_ for _ in ()).throw(RuntimeError("store down")),
        cancel=cancel_b,
    )

    report = await registry.drain(timeout=0.0, cancel_grace=1.0)

    assert order == ["persist-a", "cancel-a", "cancel-b"]
    assert len(report.callback_errors) == 1
    assert report.callback_errors[0].startswith("persist:run-b:RuntimeError")
    assert report.deadline_expired == ("job-a", "run-b")
    assert report.timed_out == ()
    assert report.clean is False
    assert registry.state is ActiveWorkState.STOPPED


@pytest.mark.asyncio
async def test_report_distinguishes_forced_work_from_still_stuck_work():
    registry = ActiveWorkRegistry()
    registry.register(
        kind="worker",
        surface="tui",
        work_id="stuck-run",
        cancel=lambda: None,
    )

    report = await registry.drain(timeout=0.0, cancel_grace=0.0)

    assert report.deadline_expired == ("stuck-run",)
    assert report.timed_out == ("stuck-run",)
    assert registry.state is ActiveWorkState.DRAINING


def test_release_and_callback_updates_are_generation_safe_and_idempotent():
    registry = ActiveWorkRegistry()
    lease = registry.register(kind="worker", surface="tui", work_id="run-1")

    assert lease.release() is True
    assert lease.release() is False
    assert lease.set_callbacks(cancel=lambda: None) is False

    registry.begin_drain()
    registry.stop()
    next_generation = registry.start_accepting()
    replacement = registry.register(kind="worker", surface="tui", work_id="run-1")
    assert replacement.generation == next_generation
    assert replacement.generation != lease.generation
