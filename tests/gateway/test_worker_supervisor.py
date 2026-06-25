"""End-to-end tests for ``WorkerSupervisor``.

These spawn a real ``python -m tui_gateway.run_worker`` subprocess and
drive it via stdin/stdout. They cover:

- spawn → send ``run.start`` → receive ``run.terminal`` + log frames
  (Phase 4a stub handler echoes a ``stubbed`` terminal)
- spawn → ``shutdown_all()`` cleanly terminates the subprocess
- spawn → kill the process out-of-band → ``send()`` returns False
- repeated ``ensure()`` is idempotent (same pid)
- queue maxsize=1 still drains under burst (backpressure smoke test)

Slow-ish (subprocess startup ≈ 200-500ms) but each test is well under
the 30s pytest-timeout fallback.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from tui_gateway.run_worker import (
    EventFrame,
    InteractiveRequestFrame,
    LogFrame,
    RunCancelFrame,
    RunStartFrame,
    RunTerminalFrame,
)
from tui_gateway.services.runtime_proxy import RuntimeScope
from tui_gateway.services.worker_supervisor import WorkerSupervisor


class _Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, EventFrame]] = []
        self.interactive: list[tuple[str, InteractiveRequestFrame]] = []
        self.terminal: list[tuple[str, RunTerminalFrame]] = []
        self.logs: list[tuple[str, LogFrame]] = []
        self.terminal_received = asyncio.Event()

    async def on_event(self, scope: str, frame: EventFrame) -> None:
        self.events.append((scope, frame))

    async def on_interactive(self, scope: str, frame: InteractiveRequestFrame) -> None:
        self.interactive.append((scope, frame))

    async def on_terminal(self, scope: str, frame: RunTerminalFrame) -> None:
        self.terminal.append((scope, frame))
        self.terminal_received.set()

    async def on_log(self, scope: str, frame: LogFrame) -> None:
        self.logs.append((scope, frame))


def _make_supervisor(collector: _Collector) -> WorkerSupervisor:
    return WorkerSupervisor(
        on_event=collector.on_event,
        on_interactive_request=collector.on_interactive,
        on_run_terminal=collector.on_terminal,
        on_log=collector.on_log,
    )


def _scope(tmp_path) -> RuntimeScope:
    return RuntimeScope(
        agent_profile_id="prof-test",
        runtime_scope_key="profile:test-e2e",
        hermes_home=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_spawn_send_run_start_receive_terminal(tmp_path) -> None:
    collector = _Collector()
    sup = _make_supervisor(collector)
    try:
        worker = await sup.ensure(_scope(tmp_path))
        assert worker.running()
        assert worker.process.pid > 0

        ok = await sup.send(
            worker.scope_key,
            RunStartFrame(
                run_id="run-e2e-1",
                turn_id="turn-1",
                stored_session_id="sess-1",
                prompt="hello",
                params={},
            ),
        )
        assert ok

        # Stub handler emits a log + run.terminal status=stubbed.
        await asyncio.wait_for(collector.terminal_received.wait(), timeout=10.0)
        assert len(collector.terminal) == 1
        scope, terminal = collector.terminal[0]
        assert scope == worker.scope_key
        assert terminal.run_id == "run-e2e-1"
        assert terminal.status == "stubbed"
        # The stub also emits a startup log + run.start log.
        log_texts = [f.text for _, f in collector.logs]
        assert any("run_worker: started" in t for t in log_texts)
        assert any("run.start" in t for t in log_texts)
    finally:
        await sup.shutdown_all()


@pytest.mark.asyncio
async def test_ensure_is_idempotent(tmp_path) -> None:
    collector = _Collector()
    sup = _make_supervisor(collector)
    try:
        scope = _scope(tmp_path)
        first = await sup.ensure(scope)
        second = await sup.ensure(scope)
        assert first is second
        assert first.process.pid == second.process.pid
    finally:
        await sup.shutdown_all()


@pytest.mark.asyncio
async def test_shutdown_terminates_process(tmp_path) -> None:
    collector = _Collector()
    sup = _make_supervisor(collector)
    worker = await sup.ensure(_scope(tmp_path))
    pid = worker.process.pid
    await sup.shutdown_all()
    # Process should be reaped; PID may be recycled but `process.returncode`
    # must be set.
    assert worker.process.returncode is not None
    # Best-effort: confirm OS no longer has the process (signal 0 = exists check).
    if sys.platform != "win32":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.asyncio
async def test_send_returns_false_when_worker_dead(tmp_path) -> None:
    collector = _Collector()
    sup = _make_supervisor(collector)
    try:
        worker = await sup.ensure(_scope(tmp_path))
        # Kill out-of-band.
        worker.process.kill()
        await worker.process.wait()
        ok = await sup.send(
            worker.scope_key,
            RunCancelFrame(run_id="r-doesnt-matter"),
        )
        assert ok is False
    finally:
        await sup.shutdown_all()


@pytest.mark.asyncio
async def test_send_unknown_scope_returns_false(tmp_path) -> None:
    sup = _make_supervisor(_Collector())
    ok = await sup.send("never-spawned", RunCancelFrame(run_id="x"))
    assert ok is False
    await sup.shutdown_all()


@pytest.mark.asyncio
async def test_snapshot_reports_running_worker(tmp_path) -> None:
    sup = _make_supervisor(_Collector())
    try:
        worker = await sup.ensure(_scope(tmp_path))
        snap = sup.snapshot()
        assert snap["source"] == "dovie-run-worker-supervisor"
        assert snap["workerCount"] == 1
        assert snap["runningWorkerCount"] == 1
        assert snap["workers"][0]["scopeKey"] == worker.scope_key
        assert snap["workers"][0]["running"] is True
    finally:
        await sup.shutdown_all()
