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
    WorkerReadyFrame,
    encode_outgoing,
)
from tui_gateway.services.runtime_scope import RuntimeScope
from hermes_agent.orchestration.worker_supervisor import (
    RunWorker,
    WorkerSupervisor,
    _DEFAULT_WORKER_STDIO_LIMIT_BYTES,
)


class _Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, EventFrame]] = []
        self.interactive: list[tuple[str, InteractiveRequestFrame]] = []
        self.terminal: list[tuple[str, RunTerminalFrame]] = []
        self.logs: list[tuple[str, LogFrame]] = []
        self.terminal_received = asyncio.Event()

    async def on_event(self, scope: str, conversation_id: str, frame: EventFrame) -> None:
        self.events.append((scope, frame))

    async def on_interactive(
        self,
        scope: str,
        conversation_id: str,
        frame: InteractiveRequestFrame,
    ) -> None:
        self.interactive.append((scope, frame))

    async def on_terminal(
        self,
        scope: str,
        conversation_id: str,
        frame: RunTerminalFrame,
    ) -> None:
        self.terminal.append((scope, frame))
        self.terminal_received.set()

    async def on_log(self, scope: str, conversation_id: str, frame: LogFrame) -> None:
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
    """End-to-end: spawn a real worker, send run.start, receive
    run.terminal.

    Phase 5c.2: the default backend now invokes ``_execute_prompt_submit``
    on a real session record. With no LLM provider configured in the
    test env, agent build fails — the runner still surfaces that as an
    ``EventFrame(type=error)`` plus a ``message.complete`` event with
    payload.status=error, and finally emits a ``RunTerminalFrame
    status=completed`` (runner returned without raising). The bridge
    + protocol round-trip is what's being verified here, not the LLM
    output.

    The startup is heavy (~5s for ``setup_worker_environment`` → import
    of ``tui_gateway.server``, plugin discovery, agent build attempt),
    so the timeout is generous."""
    collector = _Collector()
    sup = _make_supervisor(collector)
    try:
        worker = await sup.ensure(_scope(tmp_path))
        assert worker.running()
        assert worker.process.pid > 0

        ok = await sup.send(
            worker.scope_key,
            worker.conversation_id,
            RunStartFrame(
                run_id="run-e2e-1",
                turn_id="turn-1",
                conversation_session_id="sess-1",
                prompt="hello",
                params={},
            ),
        )
        assert ok

        await asyncio.wait_for(collector.terminal_received.wait(), timeout=25.0)
        assert len(collector.terminal) == 1
        scope, terminal = collector.terminal[0]
        assert scope == worker.scope_key
        assert terminal.run_id == "run-e2e-1"
        assert terminal.status == "completed"
        assert terminal.conversation_session_id == "sess-1"
        assert terminal.turn_id == "turn-1"
        # The bridge surfaced the agent-init failure as an EventFrame.
        event_payloads = [
            f.params for _, f in collector.events
            if isinstance(f.params, dict)
        ]
        error_events = [p for p in event_payloads if p.get("type") == "error"]
        assert error_events, "expected an EventFrame(type=error) surfacing the agent init failure"
        # The startup log is also emitted.
        log_texts = [f.text for _, f in collector.logs]
        assert any("run_worker: started" in t for t in log_texts)
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
@pytest.mark.live_system_guard_bypass
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
@pytest.mark.live_system_guard_bypass
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
            worker.conversation_id,
            RunCancelFrame(run_id="r-doesnt-matter"),
        )
        assert ok is False
    finally:
        await sup.shutdown_all()


@pytest.mark.asyncio
async def test_send_unknown_scope_returns_false(tmp_path) -> None:
    sup = _make_supervisor(_Collector())
    ok = await sup.send("never-spawned", "", RunCancelFrame(run_id="x"))
    assert ok is False
    await sup.shutdown_all()


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, stdout: asyncio.StreamReader) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout
        self.pid = 4242
        self.returncode = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_spawn_configures_large_worker_stdio_limit(monkeypatch, tmp_path) -> None:
    captured: dict[str, int] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["limit"] = kwargs["limit"]
        stdout = asyncio.StreamReader(limit=kwargs["limit"])
        stdout.feed_data(
            (encode_outgoing(WorkerReadyFrame(ready=True, bootstrap_ms=1.0)) + "\n").encode()
        )
        stdout.feed_eof()
        return _FakeProcess(stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    collector = _Collector()
    sup = _make_supervisor(collector)
    try:
        await sup.ensure(_scope(tmp_path))
        assert captured["limit"] == _DEFAULT_WORKER_STDIO_LIMIT_BYTES
    finally:
        await sup.shutdown_all()


@pytest.mark.asyncio
async def test_read_loop_dispatches_large_event_frame(tmp_path) -> None:
    collector = _Collector()
    sup = _make_supervisor(collector)
    stdout = asyncio.StreamReader(limit=_DEFAULT_WORKER_STDIO_LIMIT_BYTES)
    large_text = "x" * (128 * 1024)
    stdout.feed_data(
        (
            encode_outgoing(
                EventFrame(params={
                    "type": "message.complete",
                    "session_id": "sess-large",
                    "payload": {"text": large_text},
                })
            )
            + "\n"
        ).encode("utf-8")
    )
    stdout.feed_eof()
    worker = RunWorker(
        scope=_scope(tmp_path),
        process=_FakeProcess(stdout),
        inbound_queue=asyncio.Queue(maxsize=1),
        created_at=0.0,
        last_used_at=0.0,
    )

    await asyncio.gather(
        sup._read_loop(worker),
        sup._dispatch_loop(worker),
    )

    assert len(collector.events) == 1
    assert collector.events[0][0] == "profile:test-e2e"
    assert collector.events[0][1].params["payload"]["text"] == large_text


@pytest.mark.asyncio
async def test_rebind_atomically_claims_ready_worker(tmp_path) -> None:
    collector = _Collector()
    sup = _make_supervisor(collector)
    try:
        warm = await sup.ensure(_scope(tmp_path))
        target = RuntimeScope(
            agent_profile_id="prof-test",
            runtime_scope_key=warm.scope_key,
            conversation_id="conversation-1",
            hermes_home=str(tmp_path),
        )

        claimed = await sup.rebind(warm, target)

        assert claimed is warm
        assert sup.get(target.runtime_scope_key, "") is None
        assert sup.get(target.runtime_scope_key, "conversation-1") is warm
        assert warm.conversation_id == "conversation-1"
    finally:
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
