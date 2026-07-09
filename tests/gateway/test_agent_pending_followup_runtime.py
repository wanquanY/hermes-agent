import asyncio
from types import SimpleNamespace

import pytest

from channels.platforms.base import BasePlatformAdapter, SendResult
from channels.platforms.base_models import MessageEvent
from channels.session_identity import SessionSource
from hermes_gateway.agent_pending_followup_runtime import (
    PendingFollowupContext,
    agent_pending_followup_for,
)
from hermes_gateway.config import Platform, PlatformConfig


class _Adapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.sent: list[tuple[str, str]] = []
        self.typing: list[str] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="sent")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        self.typing.append(chat_id)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _Runner:
    _MAX_INTERRUPT_DEPTH = 3
    _draining = False

    def __init__(self, adapter: _Adapter) -> None:
        self.adapters = {Platform.TELEGRAM: adapter}
        self.followup_calls: list[dict] = []

    def _status_action_label(self) -> str:
        return "shutdown"

    def _reply_anchor_for_event(self, event: MessageEvent) -> str | None:
        return event.message_id

    async def _prepare_inbound_message_text(self, *, event, source, history):
        return event.text

    async def _run_agent(self, **kwargs):
        self.followup_calls.append(kwargs)
        return {"final_response": "follow-up", "messages": kwargs["history"]}


@pytest.mark.asyncio
async def test_pending_followup_uses_adapter_public_contracts():
    adapter = _Adapter()
    runner = _Runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1")
    event = MessageEvent(text="next turn", source=source, message_id="m-next")
    adapter.queue_pending_message_event("session-1", event)
    adapter._active_sessions["session-1"] = asyncio.Event()
    adapter._active_sessions["session-1"].set()
    fired = []
    adapter.register_post_delivery_callback(
        "session-1",
        lambda: fired.append("callback"),
        generation=8,
    )

    result = await agent_pending_followup_for(runner).process(
        response={"final_response": "first"},
        result={"final_response": "first", "messages": [{"role": "assistant"}]},
        context=PendingFollowupContext(
            message="original",
            context_prompt="ctx",
            history=[],
            source=source,
            session_id="sid-1",
            session_key="session-1",
            run_generation=8,
            interrupt_depth=0,
            status_thread_metadata={"thread": "t"},
            stream_consumer=SimpleNamespace(
                final_response_sent=False,
                final_content_delivered=False,
            ),
            stream_task=None,
        ),
    )

    assert result == {"final_response": "follow-up", "messages": [{"role": "assistant"}]}
    assert adapter.sent == [("chat-1", "first")]
    assert adapter.typing == ["chat-1"]
    assert fired == ["callback"]
    assert adapter.has_pending_interrupt("session-1") is False
    assert runner.followup_calls[0]["message"] == "next turn"
    assert runner.followup_calls[0]["event_message_id"] == "m-next"


@pytest.mark.asyncio
async def test_pending_followup_returns_none_without_pending_input():
    adapter = _Adapter()
    runner = _Runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1")

    result = await agent_pending_followup_for(runner).process(
        response={"final_response": "first"},
        result={"final_response": "first", "messages": []},
        context=PendingFollowupContext(
            message="original",
            context_prompt="ctx",
            history=[],
            source=source,
            session_id="sid-1",
            session_key="session-1",
            run_generation=8,
            interrupt_depth=0,
            status_thread_metadata=None,
            stream_consumer=None,
            stream_task=None,
        ),
    )

    assert result is None
    assert runner.followup_calls == []
