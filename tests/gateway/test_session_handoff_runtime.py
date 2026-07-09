from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from channels.platforms.base import SendResult
from hermes_gateway.config import HomeChannel, Platform
from hermes_gateway.session_handoff_runtime import session_handoff_runtime_for
from hermes_gateway.session import build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_process_handoff_switches_cli_session_and_dispatches_synthetic_turn():
    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].extra = {}
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-chat",
        name="Home",
    )
    adapter.create_handoff_thread = AsyncMock(return_value="thread-1")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="sent-1"))

    observed_events = []

    async def _handle_message(event):
        observed_events.append(event)
        return "handoff acknowledged"

    runner._handle_message = _handle_message
    runner._evict_cached_agent = MagicMock()
    runner._release_running_agent_state = MagicMock(return_value=True)
    runner.session_store.get_or_create_session = MagicMock()
    runner.session_store.switch_session = MagicMock(return_value=object())

    await session_handoff_runtime_for(runner).process_handoff({
        "id": "cli-session-1",
        "handoff_platform": "telegram",
        "title": "CLI Work",
    })

    assert len(observed_events) == 1
    event = observed_events[0]
    assert event.internal is True
    assert event.source.platform == Platform.TELEGRAM
    assert event.source.chat_id == "home-chat"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "thread-1"
    assert "CLI Work" in event.text

    session_key = build_session_key(event.source)
    runner.session_store.get_or_create_session.assert_called_once_with(event.source)
    runner.session_store.switch_session.assert_called_once_with(session_key, "cli-session-1")
    runner._evict_cached_agent.assert_called_once_with(session_key)
    runner._release_running_agent_state.assert_called_once_with(session_key)
    adapter.send.assert_called_once_with(
        chat_id="home-chat",
        content="handoff acknowledged",
        metadata={"thread_id": "thread-1"},
    )


@pytest.mark.asyncio
async def test_process_handoff_requires_active_platform():
    runner, _adapter = make_restart_runner()
    runner.adapters = {}

    with pytest.raises(RuntimeError, match="not active"):
        await session_handoff_runtime_for(runner).process_handoff({
            "id": "cli-session-1",
            "handoff_platform": "telegram",
        })
