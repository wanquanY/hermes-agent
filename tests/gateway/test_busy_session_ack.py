"""Tests for busy-session acknowledgment when user sends messages during active agent runs.

Verifies that users get an immediate status response instead of total silence
when the agent is working on a task. See PR fix for the @Lonely__MH report.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hermes_gateway.busy_session_runtime import busy_session_runtime_for

# ---------------------------------------------------------------------------
# Minimal stubs so we can import gateway code without heavy deps
# ---------------------------------------------------------------------------
import sys, types

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from channels.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(text="hello", chat_id="123", platform_val="telegram"):
    """Build a minimal MessageEvent."""
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    evt = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )
    return evt


def _make_runner():
    """Build a minimal GatewayRunner-like object for testing."""
    from hermes_gateway.runner import GatewayRunner, _AGENT_PENDING_SENTINEL

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner, _AGENT_PENDING_SENTINEL


def _make_adapter(platform_val="telegram"):
    """Build a minimal adapter mock."""
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value=platform_val)
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusySessionAck:
    """User sends a message while agent is running — should get acknowledgment."""

    @pytest.mark.asyncio
    async def test_handle_message_queue_mode_queues_without_interrupt(self):
        """Runner queue mode must not interrupt an active agent for text follow-ups."""
        from hermes_gateway.runner import GatewayRunner

        runner, _sentinel = _make_runner()
        adapter = _make_adapter()

        event = _make_event(text="follow up in queue mode")
        sk = build_session_key(event.source)

        running_agent = MagicMock()
        runner._busy_input_mode = "queue"
        runner._running_agents[sk] = running_agent
        runner.adapters[event.source.platform] = adapter

        result = await GatewayRunner._handle_message(runner, event)

        assert result is None
        assert sk in adapter._pending_messages
        assert adapter._pending_messages[sk] is event
        assert sk not in runner._pending_messages
        running_agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_ack_when_agent_running(self):
        """First message during busy session should get a status ack."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="Are you working?")
        sk = build_session_key(event.source)

        # Simulate running agent
        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 1.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min ago
        runner.adapters[event.source.platform] = adapter

        result = await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        assert result is True  # handled
        # Verify ack was sent
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        if not content and call_kwargs.args:
            # positional args
            content = str(call_kwargs)
        assert "Interrupting" in content or "respond" in content
        assert "/stop" not in content  # no need — we ARE interrupting

        # Verify agent interrupt was called
        agent.interrupt.assert_called_once_with("Are you working?")

    @pytest.mark.asyncio
    async def test_queue_mode_suppresses_interrupt_and_updates_ack(self):
        """When busy_input_mode is 'queue', message is queued WITHOUT interrupt."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()

        event = _make_event(text="Add this to queue")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        # VERIFY: Agent was NOT interrupted
        agent.interrupt.assert_not_called()
        assert adapter._pending_messages[sk] is event

        # VERIFY: Ack sent with queue-specific wording
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content
        assert "respond once the current task finishes" in content
        assert "Interrupting" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_calls_agent_steer_no_interrupt_no_queue(self):
        """busy_input_mode='steer' injects via agent.steer() and skips queueing."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="also check the tests")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        # VERIFY: Agent was steered, NOT interrupted
        agent.steer.assert_called_once_with("also check the tests")
        agent.interrupt.assert_not_called()

        # VERIFY: No queueing — successful steer must NOT replay as next turn
        assert sk not in adapter._pending_messages

        # VERIFY: Ack mentions steer wording
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Steered" in content or "steer" in content.lower()
        assert "Interrupting" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_rejects(self):
        """If agent.steer() returns False, fall back to queue behavior."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="empty or rejected")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=False)  # rejected
        runner._running_agents[sk] = agent

        with patch("hermes_gateway.busy_session_runtime.merge_pending_message_event") as mock_merge:
            await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        agent.steer.assert_called_once()
        agent.interrupt.assert_not_called()
        # Fell back to queue semantics: event occupies the FIFO head slot.
        assert adapter._pending_messages[sk] is event

        # Ack uses queue-mode wording (not steer, not interrupt)
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content
        assert "Steered" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_pending(self):
        """If agent is still starting (sentinel), steer mode falls back to queue."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="arrived too early")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        # Agent is still being set up — sentinel in place
        runner._running_agents[sk] = sentinel

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        # Event was queued instead of steered.
        assert adapter._pending_messages[sk] is event

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content

    @pytest.mark.asyncio
    async def test_debounce_suppresses_rapid_acks(self):
        """Second message within 30s should NOT send another ack."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event1 = _make_event(text="hello?")
        # Reuse the same source so platform mock matches
        event2 = MessageEvent(
            text="still there?",
            message_type=MessageType.TEXT,
            source=event1.source,
            message_id="msg2",
        )
        sk = build_session_key(event1.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 5,
            "max_iterations": 60,
            "current_tool": None,
            "last_activity_ts": time.time(),
            "last_activity_desc": "api_call",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 60
        runner.adapters[event1.source.platform] = adapter

        # First message — should get ack
        result1 = await busy_session_runtime_for(runner).handle_active_session_busy_message(event1, sk)
        assert result1 is True
        assert adapter._send_with_retry.call_count == 1

        # Second message within cooldown — should be queued but no ack
        result2 = await busy_session_runtime_for(runner).handle_active_session_busy_message(event2, sk)
        assert result2 is True
        assert adapter._send_with_retry.call_count == 1  # still 1, no new ack

        # But interrupt should still be called for both (since we are in interrupt mode)
        assert agent.interrupt.call_count == 2

    @pytest.mark.asyncio
    async def test_ack_after_cooldown_expires(self):
        """After 30s cooldown, a new message should send a fresh ack."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="hello?")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 10,
            "max_iterations": 60,
            "current_tool": "web_search",
            "last_activity_ts": time.time(),
            "last_activity_desc": "tool",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 120
        runner.adapters[event.source.platform] = adapter

        # First ack
        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)
        assert adapter._send_with_retry.call_count == 1

        # Fake that cooldown expired
        runner._busy_ack_ts[sk] = time.time() - 31

        # Second ack should go through
        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)
        assert adapter._send_with_retry.call_count == 2

    @pytest.mark.asyncio
    async def test_includes_status_detail(self):
        """Ack message should include iteration and tool info when available."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min
        runner.adapters[event.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "21/60" in content  # iteration
        assert "terminal" in content  # current tool
        assert "10 min" in content  # elapsed

    @pytest.mark.asyncio
    async def test_draining_still_works(self):
        """Draining case should still produce the drain-specific message."""
        runner, sentinel = _make_runner()
        runner._draining = True
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="hello")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        # Mock the drain-specific methods
        busy_session_runtime_for(runner).queue_during_drain_enabled = lambda: False
        runner._status_action_gerund = lambda: "restarting"

        result = await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)
        assert result is True

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "restarting" in content

    @pytest.mark.asyncio
    async def test_pending_sentinel_no_interrupt(self):
        """When agent is PENDING_SENTINEL, don't call interrupt (it has no method)."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="hey")
        sk = build_session_key(event.source)

        runner._running_agents[sk] = sentinel
        runner._running_agents_ts[sk] = time.time()
        runner.adapters[event.source.platform] = adapter

        result = await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)
        assert result is True
        # Should still send ack
        adapter._send_with_retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_adapter_falls_through(self):
        """If adapter is missing, return False so default path handles it."""
        runner, sentinel = _make_runner()

        event = _make_event(text="hello")
        sk = build_session_key(event.source)

        # No adapter registered
        runner._running_agents[sk] = MagicMock()

        result = await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)
        assert result is False  # not handled, let default path try

    @pytest.mark.asyncio
    async def test_local_compression_marker_demotes_interrupt_to_fifo_queue(self):
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        first = _make_event(text="first follow-up")
        second = MessageEvent(
            text="second follow-up",
            message_type=MessageType.TEXT,
            source=first.source,
            message_id="msg2",
        )
        sk = build_session_key(first.source)
        agent = MagicMock()
        agent._compression_in_flight = True
        agent.session_id = "session-1"
        runner._running_agents[sk] = agent
        runner.adapters[first.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(first, sk)
        await busy_session_runtime_for(runner).handle_active_session_busy_message(second, sk)

        agent.interrupt.assert_not_called()
        assert adapter._pending_messages[sk] is first
        assert runner._queued_events[sk] == [second]
        content = adapter._send_with_retry.call_args_list[0].kwargs["content"]
        assert "Compressing context safely" in content
        assert "queued for the next turn" in content
        assert "/stop" in content

    @pytest.mark.asyncio
    async def test_durable_compression_lease_demotes_interrupt(self):
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="follow-up")
        sk = build_session_key(event.source)
        leases = MagicMock()
        leases.holder.return_value = "compressor:worker-1"
        agent = MagicMock()
        agent._compression_in_flight = False
        agent.session_id = "session-1"
        agent._session_db = SimpleNamespace(compression_leases=leases)
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        leases.holder.assert_called_once_with("session-1")
        agent.interrupt.assert_not_called()
        assert adapter._pending_messages[sk] is event

    @pytest.mark.asyncio
    async def test_codex_native_compaction_demotes_interrupt(self):
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="follow-up during native compaction")
        sk = build_session_key(event.source)
        agent = MagicMock()
        agent._compression_in_flight = False
        agent._codex_session = SimpleNamespace(is_compacting=True)
        agent.session_id = "session-1"
        agent._session_db = None
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        agent.interrupt.assert_not_called()
        assert adapter._pending_messages[sk] is event

    @pytest.mark.asyncio
    async def test_compression_probe_error_fails_closed(self):
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="follow-up")
        sk = build_session_key(event.source)
        leases = MagicMock()
        leases.holder.side_effect = RuntimeError("database unavailable")
        agent = MagicMock()
        agent._compression_in_flight = False
        agent.session_id = "session-1"
        agent._session_db = SimpleNamespace(compression_leases=leases)
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        agent.interrupt.assert_not_called()
        assert adapter._pending_messages[sk] is event

    @pytest.mark.asyncio
    async def test_no_compression_marker_or_lease_keeps_interrupt_mode(self):
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()
        event = _make_event(text="interrupt now")
        sk = build_session_key(event.source)
        leases = MagicMock()
        leases.holder.return_value = None
        agent = MagicMock()
        agent._compression_in_flight = False
        agent.session_id = "session-1"
        agent._session_db = SimpleNamespace(compression_leases=leases)
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        agent.interrupt.assert_called_once_with("interrupt now")


class TestBusySessionOnboardingHint:
    """First-touch hint appended to the busy-ack the first time it fires."""

    @pytest.mark.asyncio
    async def test_first_busy_ack_appends_interrupt_hint(self, tmp_path, monkeypatch):
        """First busy-while-running message gets an extra hint about /busy."""
        import hermes_gateway.busy_session_runtime as _busy_runtime

        monkeypatch.setattr(_busy_runtime, "_hermes_home", tmp_path)
        # mark_seen imports utils.atomic_yaml_write; make sure it resolves
        # against a writable dir by pointing _hermes_home at tmp_path.
        monkeypatch.setattr(_busy_runtime, "_load_gateway_config", lambda: {})

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="ping")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3, "max_iterations": 60,
            "current_tool": None, "last_activity_ts": time.time(),
            "last_activity_desc": "api", "seconds_since_activity": 0.1,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 5
        runner.adapters[event.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")

        # Normal ack body
        assert "Interrupting" in content
        # First-touch hint appended
        assert "First-time tip" in content
        assert "/busy queue" in content

        # The flag is now persisted to tmp_path/config.yaml
        import yaml
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert cfg["onboarding"]["seen"]["busy_input_prompt"] is True

    @pytest.mark.asyncio
    async def test_second_busy_ack_omits_hint(self, tmp_path, monkeypatch):
        """Once the flag is marked, the hint never appears again."""
        import hermes_gateway.busy_session_runtime as _busy_runtime
        import yaml

        monkeypatch.setattr(_busy_runtime, "_hermes_home", tmp_path)
        # Pre-populate the config so is_seen() returns True from the start.
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({
            "onboarding": {"seen": {"busy_input_prompt": True}},
        }))
        monkeypatch.setattr(
            _busy_runtime, "_load_gateway_config",
            lambda: yaml.safe_load((tmp_path / "config.yaml").read_text()),
        )

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="ping again")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3, "max_iterations": 60,
            "current_tool": None, "last_activity_ts": time.time(),
            "last_activity_desc": "api", "seconds_since_activity": 0.1,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 5
        runner.adapters[event.source.platform] = adapter

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")

        assert "Interrupting" in content
        assert "First-time tip" not in content
        assert "/busy queue" not in content

    @pytest.mark.asyncio
    async def test_queue_mode_hint_points_to_interrupt(self, tmp_path, monkeypatch):
        """In queue mode the hint should suggest /busy interrupt, not /busy queue."""
        import hermes_gateway.busy_session_runtime as _busy_runtime

        monkeypatch.setattr(_busy_runtime, "_hermes_home", tmp_path)
        monkeypatch.setattr(_busy_runtime, "_load_gateway_config", lambda: {})

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()

        event = _make_event(text="queue me")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent

        await busy_session_runtime_for(runner).handle_active_session_busy_message(event, sk)

        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Queued for the next turn" in content
        assert "First-time tip" in content
        assert "/busy interrupt" in content
        # Must NOT tell the user to /busy queue when they're already on queue.
        assert "/busy queue" not in content


class TestLongRunningNotificationOwnership:
    """The long-running heartbeat must stop once its run no longer owns the
    session slot or the executor finished — otherwise a stale
    'running: delegate_task' bubble outlives the run that spawned it (#12029).
    """

    def test_notification_stops_after_session_ownership_moves(self):
        from hermes_gateway.runner import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}

        original_agent = MagicMock()
        replacement_agent = MagicMock()
        runner._running_agents["sess"] = replacement_agent

        assert runner._should_emit_long_running_notification(
            "sess", original_agent, executor_task=None
        ) is False

    def test_notification_stops_after_executor_finishes(self):
        from hermes_gateway.runner import GatewayRunner

        runner = object.__new__(GatewayRunner)
        agent = MagicMock()
        runner._running_agents = {"sess": agent}

        done_task = MagicMock()
        done_task.done.return_value = True

        assert runner._should_emit_long_running_notification(
            "sess", agent, executor_task=done_task
        ) is False

    def test_notification_stops_when_agent_is_gone(self):
        from hermes_gateway.runner import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}

        assert runner._should_emit_long_running_notification(
            "sess", None, executor_task=None
        ) is False

    def test_notification_continues_for_live_active_run(self):
        from hermes_gateway.runner import GatewayRunner

        runner = object.__new__(GatewayRunner)
        agent = MagicMock()
        runner._running_agents = {"sess": agent}

        live_task = MagicMock()
        live_task.done.return_value = False

        assert runner._should_emit_long_running_notification(
            "sess", agent, executor_task=live_task
        ) is True
