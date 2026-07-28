"""Tests that /new (and its /reset alias) clears session-scoped state safely."""
import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_gateway.config import GatewayConfig, Platform, PlatformConfig
from channels.platforms.base import MessageEvent
from hermes_gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from hermes_gateway.runner import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.reset_session.return_value = session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None  # disables _evict_cached_agent lock path
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
async def test_new_command_clears_session_model_override():
    """/new must remove the session-scoped model override for that session."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    # Simulate a prior /model switch stored as a session override
    runner._session_model_overrides[session_key] = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "***",
        "base_url": "",
        "api_mode": "openai",
    }
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._pending_model_notes[session_key] = "[Note: switched to gpt-4o.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides
    assert session_key not in runner._pending_model_notes


@pytest.mark.asyncio
async def test_new_command_no_override_is_noop():
    """/new with no prior model override must not raise."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides


@pytest.mark.asyncio
async def test_new_command_only_clears_own_session():
    """/new must only clear the override for the session that triggered it."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    other_key = "other_session_key"

    runner._session_model_overrides[session_key] = {
        "model": "gpt-4o",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "",
        "api_mode": "openai",
    }
    runner._session_model_overrides[other_key] = {
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "api_key": "***",
        "base_url": "",
        "api_mode": "anthropic",
    }
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._session_reasoning_overrides[other_key] = {"enabled": True, "effort": "low"}
    runner._pending_model_notes[session_key] = "[Note: switched to gpt-4o.]"
    runner._pending_model_notes[other_key] = "[Note: switched to claude-sonnet-4-6.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._session_model_overrides
    assert other_key in runner._session_model_overrides
    assert session_key not in runner._session_reasoning_overrides
    assert other_key in runner._session_reasoning_overrides
    assert session_key not in runner._pending_model_notes
    assert other_key in runner._pending_model_notes


@pytest.mark.asyncio
async def test_new_cleanup_does_not_block_event_loop_or_hold_cache_lock():
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    old_agent = MagicMock()
    runner._agent_cache_lock = threading.RLock()
    runner._agent_cache = {session_key: (old_agent, "signature")}

    def slow_cleanup(agent):
        assert agent is old_agent
        cleanup_started.set()
        release_cleanup.wait(timeout=2)

    runner._cleanup_agent_resources = slow_cleanup
    reset_task = asyncio.create_task(runner._handle_reset_command(_make_event("/new")))
    while not cleanup_started.is_set():
        await asyncio.sleep(0.001)

    ticks = 0
    for _ in range(20):
        ticks += 1
        await asyncio.sleep(0.002)
    assert ticks == 20
    assert not reset_task.done()
    assert runner._agent_cache_lock.acquire(timeout=0.1)
    runner._agent_cache_lock.release()
    assert session_key not in runner._agent_cache

    release_cleanup.set()
    await reset_task


@pytest.mark.asyncio
async def test_new_cleanup_timeout_still_rotates_session(monkeypatch, caplog):
    import hermes_gateway.reset_command as reset_command

    runner = _make_runner()
    session_key = build_session_key(_make_source())
    release_cleanup = threading.Event()
    runner._agent_cache_lock = threading.RLock()
    runner._agent_cache = {session_key: (MagicMock(), "signature")}
    runner._cleanup_agent_resources = lambda _agent: release_cleanup.wait(timeout=2)
    monkeypatch.setattr(reset_command, "_RESET_CLEANUP_TIMEOUT_S", 0.01)

    with caplog.at_level("WARNING", logger="hermes_gateway.reset_command"):
        await asyncio.wait_for(
            runner._handle_reset_command(_make_event("/new")),
            timeout=1,
        )
    release_cleanup.set()

    runner.session_store.reset_session.assert_called_once_with(session_key)
    assert any("proceeding while cleanup finishes off-loop" in message for message in caplog.messages)
