"""Finite-choice command picker contract across the gateway boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hermes_gateway.fast_command as fast_command
import hermes_gateway.gateway_runtime_config as gateway_runtime_config
import hermes_gateway.reasoning_command as reasoning_command
import hermes_gateway.runner as gateway_run
from channels.platforms.base import MessageEvent, SendResult
from hermes_gateway.config import Platform
from hermes_gateway.session import SessionSource


class PickerAdapter:
    def __init__(self, success: bool = True) -> None:
        self.calls: list[dict] = []
        self.success = success

    async def send_choice_picker(self, **kwargs):
        self.calls.append(kwargs)
        return SendResult(success=self.success, message_id="picker-1")


class NoPickerAdapter:
    pass


def make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="67890",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="message-1",
    )


def make_runner(adapter) -> gateway_run.GatewayRunner:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = None
    runner.session_store = None
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._service_tier = None
    runner._session_model_overrides = {}
    runner._session_service_tier_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    return runner


def patch_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(reasoning_command, "GATEWAY_HOME", tmp_path)
    monkeypatch.setattr(fast_command, "GATEWAY_HOME", tmp_path)
    monkeypatch.setattr(gateway_runtime_config, "_hermes_home", tmp_path)


@pytest.mark.asyncio
async def test_reasoning_picker_uses_canonical_choices_and_selection(tmp_path, monkeypatch):
    patch_home(monkeypatch, tmp_path)
    adapter = PickerAdapter()
    runner = make_runner(adapter)
    event = make_event("/reasoning")

    result = await reasoning_command.reasoning_command_for(
        runner
    ).handle_reasoning_command(event)

    assert result is None
    call = adapter.calls[0]
    values = [choice["value"] for choice in call["choices"]]
    assert values[:2] == ["none", "minimal"]
    assert values[-3:] == ["reset", "show", "hide"]
    assert call["metadata"]["requester_user_id"] == "12345"

    reply = await call["on_choice_selected"]("67890", "ultra")
    session_key = runner._session_key_for_source(event.source)
    assert "ultra" in reply
    assert runner._session_reasoning_overrides[session_key] == {
        "enabled": True,
        "effort": "ultra",
    }


@pytest.mark.asyncio
async def test_reasoning_picker_falls_back_to_status_card(tmp_path, monkeypatch):
    patch_home(monkeypatch, tmp_path)
    runner = make_runner(NoPickerAdapter())

    result = await reasoning_command.reasoning_command_for(
        runner
    ).handle_reasoning_command(make_event("/reasoning"))

    assert isinstance(result, str)
    assert "Reasoning" in result


@pytest.mark.asyncio
async def test_typed_reasoning_never_sends_picker(tmp_path, monkeypatch):
    patch_home(monkeypatch, tmp_path)
    adapter = PickerAdapter()
    runner = make_runner(adapter)

    result = await reasoning_command.reasoning_command_for(
        runner
    ).handle_reasoning_command(make_event("/reasoning high"))

    assert isinstance(result, str)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_fast_picker_selection_uses_same_session_scope_as_typed(tmp_path, monkeypatch):
    patch_home(monkeypatch, tmp_path)
    monkeypatch.setattr(fast_command, "load_gateway_config", lambda: {})
    monkeypatch.setattr(fast_command, "resolve_gateway_model", lambda _config=None: "gpt-5.4")
    adapter = PickerAdapter()
    runner = make_runner(adapter)

    result = await fast_command.fast_command_for(runner).handle_fast_command(
        make_event("/fast")
    )

    assert result is None
    call = adapter.calls[0]
    assert [choice["value"] for choice in call["choices"]] == ["fast", "normal"]
    reply = await call["on_choice_selected"]("67890", "fast")
    assert "FAST" in reply
    assert runner._service_tier == "priority"
    session_key = runner._session_key_for_source(make_event("/fast").source)
    assert runner._session_service_tier_overrides[session_key] == "priority"
    assert not (tmp_path / "config.yaml").exists()


@pytest.mark.asyncio
async def test_failed_native_picker_falls_back(tmp_path, monkeypatch):
    patch_home(monkeypatch, tmp_path)
    monkeypatch.setattr(fast_command, "load_gateway_config", lambda: {})
    monkeypatch.setattr(fast_command, "resolve_gateway_model", lambda _config=None: "gpt-5.4")
    runner = make_runner(PickerAdapter(success=False))

    result = await fast_command.fast_command_for(runner).handle_fast_command(
        make_event("/fast")
    )

    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_matrix_choice_picker_applies_authorized_reaction():
    from channels.platforms.matrix import MatrixAdapter

    adapter = object.__new__(MatrixAdapter)
    adapter._client = object()
    adapter._allowed_user_ids = {"@user:example.org"}
    adapter._reaction_redaction_delay_seconds = 0
    adapter._reaction_redaction_tasks = set()
    adapter._send_reaction = AsyncMock(side_effect=lambda *_args: "reaction-event")
    adapter.send = AsyncMock(
        side_effect=[
            SendResult(success=True, message_id="picker-event"),
            SendResult(success=True, message_id="confirmation-event"),
        ]
    )
    adapter._schedule_reaction_redaction = lambda *_args: None
    selected = AsyncMock(return_value="Applied")

    result = await adapter.send_choice_picker(
        chat_id="!room:example.org",
        title="Choose",
        choices=[{"value": "high", "label": "high", "is_current": False}],
        session_key="session-1",
        on_choice_selected=selected,
        metadata={"requester_user_id": "@user:example.org"},
    )
    handled = await adapter._handle_choice_picker_reaction(
        room_id="!room:example.org",
        reacts_to="picker-event",
        key="1️⃣",
        sender="@user:example.org",
    )

    assert result.success is True
    assert handled is True
    selected.assert_awaited_once_with("!room:example.org", "high")
