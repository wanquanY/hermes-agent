from __future__ import annotations

from typing import Any

import pytest

from tui_gateway.run_worker import EventFrame
from tui_gateway.services.worker_frame_router import WorkerFrameRouter


class _Sender:
    async def send(self, _scope_key: str, _conversation_id: str, _frame: Any) -> bool:
        return True


def _router():
    events: list[dict[str, Any]] = []

    def publish_event(payload: dict[str, Any], **_kwargs: Any) -> list:
        events.append(dict(payload))
        return []

    router = WorkerFrameRouter(
        sender=_Sender(),
        publish_event=publish_event,
        publish_run_terminal=lambda **_kwargs: None,
    )
    return router, events


@pytest.mark.asyncio
async def test_worker_emitted_event_runtime_scope_key_is_profile_based() -> None:
    router, events = _router()

    await router.on_event(
        "profile:agent-1",
        "conv-1",
        EventFrame(
            params={
                "type": "message.delta",
                "runtime_scope_key": "profile:agent-1",
                "conversation_id": "conv-1",
            }
        ),
    )

    assert events[0]["runtime_scope_key"] == "profile:agent-1"
    assert events[0]["runtime_scope_key"] != "conv-1"


@pytest.mark.asyncio
async def test_worker_emitted_event_conversation_id_field_present() -> None:
    router, events = _router()

    await router.on_event(
        "profile:agent-1",
        "conv-1",
        EventFrame(params={"type": "message.complete", "runtime_scope_key": "profile:agent-1"}),
    )

    assert events[0]["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_event_frame_routes_to_profile_scope_subscriber() -> None:
    router, events = _router()
    subscribed_scope = "profile:agent-1"

    await router.on_event(
        subscribed_scope,
        "conv-1",
        EventFrame(
            params={
                "type": "message.delta",
                "runtime_scope_key": subscribed_scope,
                "conversation_id": "conv-1",
            }
        ),
    )

    delivered = [
        event for event in events
        if event.get("runtime_scope_key") == subscribed_scope
    ]
    assert len(delivered) == 1
    assert delivered[0]["conversation_id"] == "conv-1"
