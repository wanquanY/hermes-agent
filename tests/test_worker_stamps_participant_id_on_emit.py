from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hermes_state import SessionDB
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.run_worker import EventFrame, OutgoingFrame
from tui_gateway.services import run_control
from hermes_agent.orchestration.worker_publish_bridge import (
    WorkerPublishBridge,
    get_active_run_context,
)


class _Sink:
    def __init__(self) -> None:
        self.frames: list[OutgoingFrame] = []
        self._lock = asyncio.Lock()

    async def emit(self, frame: OutgoingFrame) -> None:
        async with self._lock:
            self.frames.append(frame)


def _run_context(
    *,
    participant_id: str = "member:alice",
    execution_scope_key: str = "member-chat:conv-1:alice",
) -> RunContext:
    return RunContext(
        conversation_session_id="conv-1",
        participant_id=participant_id,
        activity_id="member-chat",
        activity_kind="member_chat",
        execution_scope_key=execution_scope_key,
        control_home="/tmp/hermes-control",
        execution_home="/tmp/hermes-execution",
    )


def _install_fake_run_control() -> tuple[types.ModuleType, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def publish_recorded_event(params, *args, **kwargs):
        calls.append(dict(params) if isinstance(params, dict) else {"raw": params})
        return []

    mod = types.ModuleType("tui_gateway.services.run_control")
    mod.publish_recorded_event = publish_recorded_event
    sys.modules["tui_gateway.services.run_control"] = mod
    import tui_gateway.services as services_pkg

    services_pkg.run_control = mod
    return mod, calls


@pytest.fixture
def fake_run_control():
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


async def _drain_emit(sink: _Sink, expected: int = 1) -> None:
    for _ in range(30):
        if len(sink.frames) >= expected:
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_worker_publish_wrapper_injects_participant_id_from_active_run_context(
    fake_run_control,
) -> None:
    mod, original_calls = fake_run_control
    sink = _Sink()
    context = _run_context(participant_id="member:alice")
    bridge = WorkerPublishBridge(emit=sink.emit, loop=asyncio.get_running_loop())
    bridge.install(conversation_session_id="conv-1", run_context=context)
    try:
        assert get_active_run_context() == context
        mod.publish_recorded_event({"type": "message.delta", "payload": {"delta": "hi"}})
        await _drain_emit(sink)
    finally:
        bridge.uninstall()

    event = [frame for frame in sink.frames if isinstance(frame, EventFrame)][0].params
    assert event["participant_id"] == "member:alice"
    assert event["participantId"] == "member:alice"
    assert event["payload"]["participant_id"] == "member:alice"
    assert event["payload"]["participantId"] == "member:alice"
    assert original_calls[0]["payload"]["participant_id"] == "member:alice"


@pytest.mark.asyncio
async def test_worker_publish_wrapper_no_op_when_payload_already_has_participant_id(
    fake_run_control,
) -> None:
    mod, original_calls = fake_run_control
    sink = _Sink()
    params = {
        "type": "message.delta",
        "payload": {"delta": "hi", "participant_id": "leader:conv-1"},
    }
    bridge = WorkerPublishBridge(emit=sink.emit, loop=asyncio.get_running_loop())
    bridge.install(conversation_session_id="conv-1", run_context=_run_context())
    try:
        mod.publish_recorded_event(params)
        await _drain_emit(sink)
    finally:
        bridge.uninstall()

    event = [frame for frame in sink.frames if isinstance(frame, EventFrame)][0].params
    assert event == params
    assert original_calls == [params]


@pytest.mark.asyncio
async def test_worker_publish_wrapper_no_op_when_no_active_run_context(
    fake_run_control,
) -> None:
    mod, original_calls = fake_run_control
    sink = _Sink()
    params = {"type": "message.delta", "payload": {"delta": "hi"}}
    bridge = WorkerPublishBridge(emit=sink.emit, loop=asyncio.get_running_loop())
    bridge.install(conversation_session_id="conv-1")
    try:
        assert get_active_run_context() is None
        mod.publish_recorded_event(params)
        await _drain_emit(sink)
    finally:
        bridge.uninstall()

    event = [frame for frame in sink.frames if isinstance(frame, EventFrame)][0].params
    assert event == params
    assert original_calls == [params]


@pytest.mark.asyncio
async def test_member_chat_event_stamped_member_not_leader_via_worker_wrap(
    fake_run_control,
) -> None:
    mod, _original_calls = fake_run_control
    sink = _Sink()
    context = _run_context(
        participant_id="member:member-alice",
        execution_scope_key="team:conv-1:leader-conversation",
    )
    bridge = WorkerPublishBridge(emit=sink.emit, loop=asyncio.get_running_loop())
    bridge.install(conversation_session_id="conv-1", run_context=context)
    try:
        mod.publish_recorded_event(
            {
                "type": "message.complete",
                "conversation_session_id": "conv-1",
                "run_id": "run-member",
                "runtime_scope_key": "team:conv-1:leader-conversation",
                "payload": {
                    "text": "member reply",
                    "runtime_scope_key": "team:conv-1:leader-conversation",
                },
            }
        )
        await _drain_emit(sink)
    finally:
        bridge.uninstall()

    event = [frame for frame in sink.frames if isinstance(frame, EventFrame)][0].params
    assert event["payload"]["participant_id"] == "member:member-alice"
    assert event["participant_id"] == "member:member-alice"
    assert event["payload"]["participant_id"] != "leader:conv-1"


def test_main_record_event_uses_payload_participant_id_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("conv-1", source="team_mission", transient=False)

    def fail_scope_fallback(_scope: str) -> str:
        raise AssertionError("scope fallback should not run when payload has participant_id")

    monkeypatch.setattr(run_control, "_participant_id_from_scope", fail_scope_fallback)
    try:
        run_control.record_event(
            {
                "type": "message.complete",
                "session_id": "conv-1",
                "conversation_session_id": "conv-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "team:conv-1:leader-conversation",
                "seq": 1,
                "payload": {
                    "text": "member reply",
                    "participant_id": "member:member-alice",
                    "runtime_scope_key": "team:conv-1:leader-conversation",
                },
            },
            db=db,
        )
        stored = db.list_run_events("conv-1", run_id="run-1")[0]
    finally:
        db.close()

    assert stored["participant_id"] == "member:member-alice"
    assert stored["participantId"] == "member:member-alice"
    assert stored["payload"]["participant_id"] == "member:member-alice"
