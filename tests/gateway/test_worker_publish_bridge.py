"""Unit tests for ``WorkerPublishBridge`` — Phase 5b.2 publish hook.

Verifies the bridge:
- Wraps ``run_control.publish_recorded_event`` preserving original behavior
- Wraps ``tools.clarify_gateway.register`` similarly
- Wraps ``tools.approval.submit_pending`` similarly
- Routes emits from a background thread onto the asyncio loop safely
- Double-install raises
- Uninstall fully restores originals
- Missing target modules degrade gracefully (no hook installed, no crash)
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
    InteractiveRequestFrame,
    OutgoingFrame,
)
from tui_gateway.services.worker_publish_bridge import WorkerPublishBridge


# Helpers -----------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.frames: list[OutgoingFrame] = []
        self._lock = asyncio.Lock()

    async def emit(self, frame: OutgoingFrame) -> None:
        async with self._lock:
            self.frames.append(frame)


def _install_fake_run_control() -> tuple[types.ModuleType, list[dict]]:
    """Install a stand-in for ``tui_gateway.services.run_control`` with
    ``publish_recorded_event`` capturing calls. Returns (module, calls).

    ``from tui_gateway.services import run_control`` reads the attribute
    from the parent package (loaded once) BEFORE falling back to
    sys.modules, so we must overwrite both for the fake to win."""
    calls: list[dict] = []

    def publish_recorded_event(params, *args, **kwargs):
        calls.append(dict(params) if isinstance(params, dict) else {"raw": params})
        return []

    mod = types.ModuleType("tui_gateway.services.run_control")
    mod.publish_recorded_event = publish_recorded_event
    sys.modules["tui_gateway.services.run_control"] = mod
    import tui_gateway.services as services_pkg
    services_pkg.run_control = mod
    return mod, calls


def _install_fake_clarify_gateway() -> tuple[types.ModuleType, list[tuple]]:
    calls: list[tuple] = []

    class _Entry:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def register(clarify_id, session_key, question, choices):
        calls.append((clarify_id, session_key, question, list(choices) if choices else None))
        return _Entry(clarify_id=clarify_id, session_key=session_key)

    mod = types.ModuleType("tools.clarify_gateway")
    mod.register = register
    sys.modules["tools.clarify_gateway"] = mod
    # Also need the package shell
    tools_pkg = sys.modules.setdefault("tools", types.ModuleType("tools"))
    setattr(tools_pkg, "clarify_gateway", mod)
    return mod, calls


def _install_fake_approval() -> tuple[types.ModuleType, list[tuple]]:
    calls: list[tuple] = []

    def submit_pending(session_key, approval_data):
        calls.append((session_key, dict(approval_data)))

    mod = types.ModuleType("tools.approval")
    mod.submit_pending = submit_pending
    sys.modules["tools.approval"] = mod
    tools_pkg = sys.modules.setdefault("tools", types.ModuleType("tools"))
    setattr(tools_pkg, "approval", mod)
    return mod, calls


@pytest.fixture
def fake_run_control(monkeypatch):
    original_module = sys.modules.get("tui_gateway.services.run_control")
    import tui_gateway.services as services_pkg
    original_attr = getattr(services_pkg, "run_control", None)
    mod, calls = _install_fake_run_control()
    yield mod, calls
    if original_module is not None:
        sys.modules["tui_gateway.services.run_control"] = original_module
    else:
        sys.modules.pop("tui_gateway.services.run_control", None)
    if original_attr is not None:
        services_pkg.run_control = original_attr
    elif hasattr(services_pkg, "run_control"):
        delattr(services_pkg, "run_control")


@pytest.fixture
def fake_clarify(monkeypatch):
    original = sys.modules.get("tools.clarify_gateway")
    mod, calls = _install_fake_clarify_gateway()
    yield mod, calls
    if original is not None:
        sys.modules["tools.clarify_gateway"] = original
    else:
        sys.modules.pop("tools.clarify_gateway", None)


@pytest.fixture
def fake_approval(monkeypatch):
    original = sys.modules.get("tools.approval")
    mod, calls = _install_fake_approval()
    yield mod, calls
    if original is not None:
        sys.modules["tools.approval"] = original
    else:
        sys.modules.pop("tools.approval", None)


# Tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_hook_emits_event_and_preserves_original(fake_run_control) -> None:
    mod, original_calls = fake_run_control
    sink = _Sink()
    loop = asyncio.get_running_loop()
    bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
    bridge.install()
    try:
        mod.publish_recorded_event({"type": "message.delta", "text": "x"})
        # Give the loop a tick to drain the threadsafe schedule.
        await asyncio.sleep(0.05)
    finally:
        bridge.uninstall()
    assert original_calls == [{"type": "message.delta", "text": "x"}]
    events = [f for f in sink.frames if isinstance(f, EventFrame)]
    assert events == [EventFrame(params={"type": "message.delta", "text": "x"})]


@pytest.mark.asyncio
async def test_publish_hook_threadsafe_from_background_thread(fake_run_control) -> None:
    mod, _ = fake_run_control
    sink = _Sink()
    loop = asyncio.get_running_loop()
    bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
    bridge.install()
    try:
        # Simulate agent thread publishing while the asyncio loop is running.
        def background() -> None:
            for i in range(5):
                mod.publish_recorded_event({"type": "message.delta", "seq": i})

        t = threading.Thread(target=background, daemon=True)
        t.start()
        # Drive the loop until threadsafe schedules complete.
        for _ in range(30):
            if len(sink.frames) >= 5:
                break
            await asyncio.sleep(0.05)
        t.join(timeout=5.0)
    finally:
        bridge.uninstall()
    seq_values = [f.params["seq"] for f in sink.frames if isinstance(f, EventFrame)]
    assert sorted(seq_values) == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_clarify_hook_emits_interactive_request(fake_clarify) -> None:
    mod, original_calls = fake_clarify
    sink = _Sink()
    loop = asyncio.get_running_loop()
    bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
    bridge.install(stored_session_id="sess-1")
    try:
        entry = mod.register("clr-1", "sess-1", "Pick one", ["A", "B"])
        await asyncio.sleep(0.05)
    finally:
        bridge.uninstall()
    assert original_calls == [("clr-1", "sess-1", "Pick one", ["A", "B"])]
    assert getattr(entry, "clarify_id") == "clr-1"
    interactives = [f for f in sink.frames if isinstance(f, InteractiveRequestFrame)]
    assert interactives == [
        InteractiveRequestFrame(
            kind="clarify",
            request_id="clr-1",
            payload={
                "clarify_id": "clr-1",
                "request_id": "clr-1",
                "session_key": "sess-1",
                "question": "Pick one",
                "choices": ["A", "B"],
            },
            stored_session_id="sess-1",
        )
    ]


@pytest.mark.asyncio
async def test_approval_hook_emits_interactive_request(fake_approval) -> None:
    mod, original_calls = fake_approval
    sink = _Sink()
    loop = asyncio.get_running_loop()
    bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
    bridge.install(stored_session_id="sess-A")
    try:
        mod.submit_pending("sess-A", {"command": "rm -rf /", "description": "danger"})
        await asyncio.sleep(0.05)
    finally:
        bridge.uninstall()
    assert original_calls == [("sess-A", {"command": "rm -rf /", "description": "danger"})]
    interactives = [f for f in sink.frames if isinstance(f, InteractiveRequestFrame)]
    assert interactives == [
        InteractiveRequestFrame(
            kind="approval",
            request_id="sess-A",
            payload={"command": "rm -rf /", "description": "danger"},
            stored_session_id="sess-A",
        )
    ]


@pytest.mark.asyncio
async def test_double_install_raises(fake_run_control) -> None:
    sink = _Sink()
    loop = asyncio.get_running_loop()
    bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
    bridge.install()
    try:
        with pytest.raises(RuntimeError, match="already installed"):
            bridge.install()
    finally:
        bridge.uninstall()


@pytest.mark.asyncio
async def test_uninstall_restores_originals(fake_run_control, fake_clarify, fake_approval) -> None:
    rc_mod, _ = fake_run_control
    clarify_mod, _ = fake_clarify
    approval_mod, _ = fake_approval
    original_publish = rc_mod.publish_recorded_event
    original_register = clarify_mod.register
    original_submit = approval_mod.submit_pending

    sink = _Sink()
    loop = asyncio.get_running_loop()
    bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
    bridge.install()
    assert rc_mod.publish_recorded_event is not original_publish
    assert clarify_mod.register is not original_register
    assert approval_mod.submit_pending is not original_submit

    bridge.uninstall()
    assert rc_mod.publish_recorded_event is original_publish
    assert clarify_mod.register is original_register
    assert approval_mod.submit_pending is original_submit


@pytest.mark.asyncio
async def test_uninstall_is_idempotent(fake_run_control) -> None:
    sink = _Sink()
    loop = asyncio.get_running_loop()
    bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
    bridge.install()
    bridge.uninstall()
    # Second uninstall must be a no-op, not raise.
    bridge.uninstall()
    assert bridge.installed is False


@pytest.mark.asyncio
async def test_missing_modules_dont_crash() -> None:
    """When tools.clarify_gateway / tools.approval aren't importable
    (CLI-only contexts), install should still complete and the publish
    hook should be in place."""
    # Force tools modules to be unavailable for this test.
    saved_clarify = sys.modules.pop("tools.clarify_gateway", None)
    saved_approval = sys.modules.pop("tools.approval", None)
    # Sabotage import.
    import builtins
    real_import = builtins.__import__

    def reject(name, *args, **kwargs):
        if name in ("tools.clarify_gateway", "tools.approval"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = reject
    try:
        # Also install fake run_control so publish hook does land.
        rc_mod, _ = _install_fake_run_control()
        sink = _Sink()
        loop = asyncio.get_running_loop()
        bridge = WorkerPublishBridge(emit=sink.emit, loop=loop)
        bridge.install()  # must not raise
        assert bridge.installed
        bridge.uninstall()
    finally:
        builtins.__import__ = real_import
        if saved_clarify is not None:
            sys.modules["tools.clarify_gateway"] = saved_clarify
        if saved_approval is not None:
            sys.modules["tools.approval"] = saved_approval
        sys.modules.pop("tui_gateway.services.run_control", None)
