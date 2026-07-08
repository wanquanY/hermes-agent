"""Unit tests for ``AgentRunBackend`` — Phase 5b.2.

Uses a stub ``AgentRunner`` to avoid spinning up the real LLM stack.
The backend's job is process management:

- Install bridge → run agent on background thread → uninstall bridge
- Emit a ``RunTerminalFrame`` whose status matches outcome
- Forward cancel intent via the threading.Event
- Refuse a second concurrent run
- Best-effort shutdown waits for the active thread
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from typing import Any

import pytest

from tui_gateway.run_worker import (
    EventFrame,
    LogFrame,
    RunStartFrame,
    RunTerminalFrame,
)
from tui_gateway.services.agent_run_backend import AgentRunBackend


class _Sink:
    def __init__(self) -> None:
        self.frames: list = []

    async def emit(self, frame) -> None:
        self.frames.append(frame)


def _scrub_run_control_fake():
    """Ensure tests/gateway/test_worker_publish_bridge.py didn't leave
    a fake ``tui_gateway.services.run_control`` in sys.modules — that
    would break the real WorkerPublishBridge ``publish`` hook lookup."""
    mod = sys.modules.get("tui_gateway.services.run_control")
    if mod is not None and getattr(mod, "__file__", None) is None:
        sys.modules.pop("tui_gateway.services.run_control", None)


@pytest.fixture(autouse=True)
def _clean_run_control():
    _scrub_run_control_fake()
    yield
    _scrub_run_control_fake()


def _start_frame(run_id: str = "r1") -> RunStartFrame:
    return RunStartFrame(
        run_id=run_id, turn_id="t1", conversation_session_id="s1", prompt="hi",
    )


@pytest.mark.asyncio
async def test_completes_emits_completed_terminal() -> None:
    def runner(frame: RunStartFrame, cancel: threading.Event) -> None:
        # Real agent would run here; stub just exits cleanly.
        time.sleep(0.05)

    backend = AgentRunBackend(runner=runner)
    sink = _Sink()
    await backend.start(_start_frame(), sink.emit)
    terms = [f for f in sink.frames if isinstance(f, RunTerminalFrame)]
    assert len(terms) == 1
    assert terms[0].status == "completed"
    assert terms[0].run_id == "r1"
    assert terms[0].conversation_session_id == "s1"


@pytest.mark.asyncio
async def test_runner_exception_emits_failed_terminal_and_log() -> None:
    def runner(frame, cancel) -> None:
        raise RuntimeError("kaboom")

    backend = AgentRunBackend(runner=runner)
    sink = _Sink()
    await backend.start(_start_frame(), sink.emit)
    terms = [f for f in sink.frames if isinstance(f, RunTerminalFrame)]
    assert len(terms) == 1
    assert terms[0].status == "failed"
    assert "kaboom" in terms[0].message
    logs = [f for f in sink.frames if isinstance(f, LogFrame)]
    assert any("agent crashed" in f.text for f in logs)


@pytest.mark.asyncio
async def test_cancel_marks_terminal_cancelled() -> None:
    cancel_seen_in_runner = threading.Event()

    def runner(frame, cancel) -> None:
        # Loop until cancel is set; raise to signal cancel cleanly.
        for _ in range(100):
            if cancel.is_set():
                cancel_seen_in_runner.set()
                raise RuntimeError("cancelled by user")
            time.sleep(0.02)

    backend = AgentRunBackend(runner=runner)
    sink = _Sink()

    # Start the run in the background, then cancel from the main coro.
    start_task = asyncio.create_task(backend.start(_start_frame("r-cancel"), sink.emit))
    # Wait until the runner thread is actually alive.
    for _ in range(50):
        if backend._active is not None:
            break
        await asyncio.sleep(0.02)
    assert backend._active is not None
    await backend.cancel("r-cancel")
    await start_task

    assert cancel_seen_in_runner.is_set()
    terms = [f for f in sink.frames if isinstance(f, RunTerminalFrame)]
    assert terms[0].status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_unknown_run_is_noop() -> None:
    backend = AgentRunBackend(runner=lambda f, c: None)
    # No active run — cancel should not raise.
    await backend.cancel("nonexistent")


@pytest.mark.asyncio
async def test_refuses_concurrent_run() -> None:
    runner_started = threading.Event()
    runner_release = threading.Event()

    def runner(frame, cancel) -> None:
        runner_started.set()
        runner_release.wait(timeout=2.0)

    backend = AgentRunBackend(runner=runner)
    sink_a = _Sink()
    sink_b = _Sink()

    task_a = asyncio.create_task(backend.start(_start_frame("r-a"), sink_a.emit))
    # Wait until run-a is actually running.
    for _ in range(50):
        if runner_started.is_set():
            break
        await asyncio.sleep(0.02)
    assert runner_started.is_set()

    # Now attempt a second concurrent run — it must refuse immediately.
    await backend.start(_start_frame("r-b"), sink_b.emit)
    terms_b = [f for f in sink_b.frames if isinstance(f, RunTerminalFrame)]
    assert len(terms_b) == 1
    assert terms_b[0].status == "failed"
    assert "another run already active" in terms_b[0].message

    # Release run-a and confirm it completes normally.
    runner_release.set()
    await task_a
    terms_a = [f for f in sink_a.frames if isinstance(f, RunTerminalFrame)]
    assert terms_a[0].status == "completed"


@pytest.mark.asyncio
async def test_shutdown_signals_cancel_and_joins() -> None:
    cancel_seen = threading.Event()
    runner_started = threading.Event()

    def runner(frame, cancel) -> None:
        runner_started.set()
        # Spin tight; exit as soon as cancel is set.
        for _ in range(200):
            if cancel.is_set():
                cancel_seen.set()
                return
            time.sleep(0.02)

    backend = AgentRunBackend(runner=runner)
    sink = _Sink()
    task = asyncio.create_task(backend.start(_start_frame("r-shut"), sink.emit))
    for _ in range(50):
        if runner_started.is_set():
            break
        await asyncio.sleep(0.02)
    await backend.shutdown()
    await task
    assert cancel_seen.is_set()
    # cancel_event was set (by shutdown) — ``_classify_outcome`` now
    # checks cancel BEFORE the exc-is-None branch, so the runner's
    # clean exit still classifies as cancelled. This is the correct
    # contract: any run that completed because cancel was signaled is
    # ``cancelled``, not ``completed``, regardless of whether the
    # cooperative interrupt surfaced as an exception or as a clean
    # return.
    terms = [f for f in sink.frames if isinstance(f, RunTerminalFrame)]
    assert terms[0].status == "cancelled"


@pytest.mark.asyncio
async def test_bridge_installs_and_uninstalls(monkeypatch) -> None:
    """Smoke test: the backend installs ``WorkerPublishBridge`` on
    start and removes it on exit. We assert by patching the bridge's
    install/uninstall to record calls."""
    install_calls: list[str] = []
    uninstall_calls: list[int] = []

    real_install_method = None

    import tui_gateway.services.worker_publish_bridge as bridge_mod
    original_cls = bridge_mod.WorkerPublishBridge

    class _RecordingBridge(original_cls):
        def install(self, *, conversation_session_id: str = "", run_context=None):
            install_calls.append(conversation_session_id)
            return super().install(conversation_session_id=conversation_session_id, run_context=run_context)

        def uninstall(self):
            uninstall_calls.append(1)
            return super().uninstall()

    # Patch the symbol the backend imports.
    monkeypatch.setattr(
        "tui_gateway.services.agent_run_backend.WorkerPublishBridge",
        _RecordingBridge,
    )

    backend = AgentRunBackend(runner=lambda f, c: None)
    sink = _Sink()
    await backend.start(_start_frame("r-bridge"), sink.emit)
    assert install_calls == ["s1"]
    # Uninstalled once at end of run.
    assert sum(uninstall_calls) >= 1
