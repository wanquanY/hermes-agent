"""Gateway producer and startup recovery integration for delivery obligations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from channels.config import Platform, PlatformConfig
from channels.platforms.base import BasePlatformAdapter
from channels.platforms.base_models import (
    EphemeralReply,
    MessageEvent,
    MessageType,
    SendResult,
)
from channels.session_identity import SessionSource
from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_agent.domain.delivery_obligation import RECOVERED_DELIVERY_MARKER
from hermes_gateway.config import GatewayConfig
from hermes_gateway.delivery_obligation_runtime import (
    GatewayDeliveryObligationService,
)


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.sent: list[tuple[str, str, object, object]] = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id="out-1")


def _event(text: str = "hello") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="C1",
            chat_type="channel",
            thread_id="T1",
        ),
        message_id="msg-1",
    )


def _runner(store, adapter):
    session_store = SimpleNamespace(clear_resume_pending=Mock(return_value=True))
    runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False),
        _session_db=store,
        adapters={Platform.SLACK: adapter},
        _profile_adapters={},
        session_store=session_store,
        _adapter_for_source=lambda _source: adapter,
    )
    adapter.gateway_runner = runner
    return runner


async def _process(adapter, response, event=None):
    event = event or _event()
    adapter._message_handler = AsyncMock(return_value=response)
    session_key = "agent:main:slack:channel:C1:T1"
    adapter._active_sessions[session_key] = asyncio.Event()
    await adapter._process_message_background(event, session_key)


@pytest.mark.asyncio
async def test_normal_final_response_is_recorded_and_delivered(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    adapter = _Adapter()
    _runner(store, adapter)
    try:
        await _process(adapter, "final answer")
        rows = store.delivery_obligations.list_recent()
    finally:
        store.close()

    assert [item[1] for item in adapter.sent] == ["final answer"]
    assert len(rows) == 1
    assert rows[0].state.value == "delivered"
    assert rows[0].metadata["thread_id"] == "T1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "response"),
    [
        (_event("/status"), "status reply"),
        (_event("!status"), "status reply"),
        (_event(), EphemeralReply("notice", ttl_seconds=10)),
        (_event(), ""),
    ],
)
async def test_non_durable_response_classes_are_not_recorded(
    tmp_path,
    event,
    response,
):
    store = open_cli_session_store(tmp_path / "state.db")
    adapter = _Adapter()
    adapter.typed_command_prefix = "!"
    _runner(store, adapter)
    try:
        await _process(adapter, response, event)
        rows = store.delivery_obligations.list_recent()
    finally:
        store.close()

    assert rows == []


@pytest.mark.asyncio
async def test_ledger_failure_never_blocks_platform_send(tmp_path, monkeypatch):
    store = open_cli_session_store(tmp_path / "state.db")
    adapter = _Adapter()
    _runner(store, adapter)
    monkeypatch.setattr(
        store.delivery_obligations,
        "record",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    try:
        await _process(adapter, "final answer")
    finally:
        store.close()

    assert [item[1] for item in adapter.sent] == ["final answer"]


@pytest.mark.asyncio
async def test_startup_recovery_marks_ambiguous_send_and_clears_resume(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    store = open_cli_session_store(home / "state.db")
    adapter = _Adapter()
    runner = _runner(store, adapter)
    service = GatewayDeliveryObligationService(runner)
    obligation_id = store.delivery_obligations.record(
        session_key="agent:main:slack:channel:C1:T1",
        inbound_message_id="msg-1",
        platform="slack",
        chat_id="C1",
        thread_id="T1",
        reply_to="msg-1",
        metadata={"thread_id": "T1", "notify": True},
        content="final answer",
        owner_pid=999_999_999,
        owner_started_at=1,
    )
    store.delivery_obligations.mark_attempting(obligation_id)
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", home)],
    )
    try:
        recovered = await service.recover_startup()
        rows = store.delivery_obligations.list_recent()
    finally:
        store.close()

    assert recovered == 1
    assert adapter.sent[0][1] == RECOVERED_DELIVERY_MARKER + "final answer"
    assert adapter.sent[0][2] == "msg-1"
    assert adapter.sent[0][3] == {"thread_id": "T1", "notify": True}
    assert rows[0].state.value == "delivered"
    runner.session_store.clear_resume_pending.assert_called_once_with(
        "agent:main:slack:channel:C1:T1"
    )


@pytest.mark.asyncio
async def test_startup_recovery_does_not_claim_disconnected_platform(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    store = open_cli_session_store(home / "state.db")
    adapter = _Adapter()
    runner = _runner(store, adapter)
    runner.adapters = {}
    runner._adapter_for_source = lambda _source: None
    service = GatewayDeliveryObligationService(runner)
    obligation_id = store.delivery_obligations.record(
        session_key="agent:main:slack:channel:C1:T1",
        inbound_message_id="msg-1",
        platform="slack",
        chat_id="C1",
        thread_id="T1",
        reply_to="msg-1",
        metadata={"thread_id": "T1"},
        content="final answer",
        owner_pid=999_999_999,
        owner_started_at=1,
    )
    store.delivery_obligations.mark_attempting(obligation_id)
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", home)],
    )
    try:
        for _ in range(store.delivery_obligations.MAX_ATTEMPTS + 1):
            assert await service.recover_startup() == 0
        row = store.delivery_obligations.list_recent()[0]
    finally:
        store.close()

    assert row.attempts == 0
    assert row.state.value == "attempting"
    runner.session_store.clear_resume_pending.assert_not_called()
