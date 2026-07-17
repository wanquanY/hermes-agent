"""Live-gateway intentional-silence protocol acceptance tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_gateway.agent_pending_followup_runtime import (
    AgentPendingFollowupRuntime,
    PendingFollowupContext,
)
from hermes_gateway.agent_turn_persistence import GatewayAgentTurnPersistenceService
from hermes_gateway.response_filters import (
    LIVE_GATEWAY_SILENT_MARKERS,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
    is_partial_silence_marker,
)
from hermes_gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


@pytest.mark.parametrize(
    "text",
    ["N", "NO", "NO_", "NO_REPLY", "NO REPLY", "[", "[SIL", "[SILENT]", "sil"],
)
def test_partial_silence_marker_positive(text):
    assert is_partial_silence_marker(text)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "NO_REPLYING", "Nope", "Hello", "The NO_REPLY token", "x" * 65],
)
def test_partial_silence_marker_negative(text):
    assert not is_partial_silence_marker(text)


def test_exact_predicate_is_success_only_and_whole_response_only():
    for marker in LIVE_GATEWAY_SILENT_MARKERS:
        assert is_intentional_silence_response(marker)
        assert is_intentional_silence_agent_result({}, marker)
        assert not is_intentional_silence_agent_result({"failed": True}, marker)
    assert not is_intentional_silence_response("The NO_REPLY token means silence")
    assert not is_intentional_silence_response("")


def _adapter(*, supports_delete: bool = True):
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="preview-1")
    )
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True))
    if supports_delete:
        adapter.delete_message = AsyncMock(return_value=True)
    else:
        del adapter.delete_message
    return adapter


def _visible_text(adapter) -> str:
    calls = list(adapter.send.call_args_list) + list(adapter.edit_message.call_args_list)
    return "\n".join(str(call.kwargs.get("content", "")) for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
async def test_exact_stream_marker_never_reaches_platform(marker):
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(edit_interval=0.001, buffer_threshold=1),
    )
    consumer.on_delta(marker)
    consumer.finish()
    await consumer.run()

    assert marker not in _visible_text(adapter)
    assert not consumer.already_sent
    assert not consumer.final_response_sent
    assert not consumer.final_content_delivered


@pytest.mark.asyncio
async def test_partial_marker_is_held_until_stream_resolves():
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(edit_interval=0.001, buffer_threshold=1),
    )
    run_task = asyncio.create_task(consumer.run())
    consumer.on_delta("NO_")
    await asyncio.sleep(0.03)
    adapter.send.assert_not_awaited()

    consumer.on_delta("REPLY")
    consumer.finish()
    await run_task
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_like_prefix_that_becomes_prose_is_delivered():
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(edit_interval=0.001, buffer_threshold=1),
    )
    consumer.on_delta("NO REPLY")
    consumer.on_delta(" needed; the task is already complete.")
    consumer.finish()
    await consumer.run()

    assert "task is already complete" in _visible_text(adapter)
    assert consumer.final_content_delivered


@pytest.mark.asyncio
async def test_existing_preview_is_retracted_and_flags_are_cleared():
    adapter = _adapter()
    consumer = GatewayStreamConsumer(adapter, "chat-1")
    consumer._message_id = "preview-1"
    consumer._preview_message_ids = {"preview-1"}
    consumer._already_sent = True
    consumer.on_delta("NO_REPLY")
    consumer.finish()
    await consumer.run()

    adapter.delete_message.assert_awaited_once_with("chat-1", "preview-1")
    assert not consumer.already_sent
    assert not consumer.final_response_sent


@pytest.mark.asyncio
async def test_missing_delete_support_is_best_effort():
    adapter = _adapter(supports_delete=False)
    consumer = GatewayStreamConsumer(adapter, "chat-1")
    consumer.on_delta("NO_REPLY")
    consumer.finish()
    await consumer.run()
    assert not consumer.final_content_delivered


def test_control_marker_is_persisted_before_outbound_filtering():
    writes = GatewayAgentTurnPersistenceService._transcript_writes(
        event=SimpleNamespace(message_id="in-1"),
        source=SimpleNamespace(platform=SimpleNamespace(value="telegram")),
        history=[{"role": "user", "content": "before"}],
        message_text="do not notify me",
        response="NO_REPLY",
        agent_result={"history_offset": 1},
        agent_messages=[],
        timestamp="now",
        context_overflow=False,
        agent_failed_early=False,
        agent_persisted=False,
    )
    assert any(
        entry.get("role") == "assistant" and entry.get("content") == "NO_REPLY"
        for entry, _skip_db in writes
    )


@pytest.mark.asyncio
async def test_queued_followup_does_not_resend_marker():
    adapter = _adapter()
    runner = SimpleNamespace(adapters={"telegram": adapter})
    runtime = AgentPendingFollowupRuntime(runner)
    context = PendingFollowupContext(
        message="next",
        context_prompt="",
        history=[],
        source=SimpleNamespace(platform="telegram", chat_id="chat-1"),
        session_id="session-1",
        session_key="key-1",
        run_generation=1,
        interrupt_depth=0,
        status_thread_metadata=None,
        stream_consumer=None,
        stream_task=None,
    )
    await runtime._deliver_first_response_if_needed(
        adapter,
        {"final_response": "NO_REPLY"},
        context,
    )
    adapter.send.assert_not_awaited()
