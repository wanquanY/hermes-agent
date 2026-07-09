import asyncio
from types import SimpleNamespace

import pytest

from hermes_gateway.agent_turn_completion import (
    AgentTurnCleanupContext,
    agent_final_delivery_for,
    agent_turn_cleanup_for,
)


class _ToolProgress:
    def __init__(self) -> None:
        self.calls = []

    def register_cleanup_callback(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Runner:
    _draining = False


@pytest.mark.asyncio
async def test_turn_cleanup_cancels_tasks_without_stream_consumer():
    runner = _Runner()
    tasks = []

    async def wait_forever():
        while True:
            await asyncio.sleep(10)

    for _ in range(5):
        tasks.append(asyncio.create_task(wait_forever()))

    await agent_turn_cleanup_for(runner).cleanup(
        AgentTurnCleanupContext(
            progress_task=tasks[0],
            stream_task=tasks[1],
            stream_consumer=None,
            interrupt_monitor=tasks[2],
            tracking_task=tasks[3],
            notify_task=tasks[4],
            session_key=None,
            run_generation=1,
        )
    )

    assert all(task.cancelled() for task in tasks)


@pytest.mark.asyncio
async def test_final_delivery_marks_already_sent_when_stream_delivered():
    response = {"final_response": "done"}
    tool_progress = _ToolProgress()

    returned = agent_final_delivery_for().mark_stream_delivery_and_register_cleanup(
        response=response,
        stream_consumer=SimpleNamespace(
            final_response_sent=True,
            final_content_delivered=False,
        ),
        session_key="session-1",
        run_generation=3,
        tool_progress=tool_progress,
    )

    assert returned is response
    assert response["already_sent"] is True
    assert tool_progress.calls[0]["session_key"] == "session-1"
    assert tool_progress.calls[0]["run_generation"] == 3


@pytest.mark.asyncio
async def test_final_delivery_does_not_suppress_empty_or_transformed_response():
    for response in (
        {"final_response": "(empty)"},
        {"final_response": "done", "response_transformed": True},
        {"final_response": "done", "failed": True},
    ):
        agent_final_delivery_for().mark_stream_delivery_and_register_cleanup(
            response=response,
            stream_consumer=SimpleNamespace(
                final_response_sent=True,
                final_content_delivered=True,
            ),
            session_key="session-1",
            run_generation=3,
            tool_progress=_ToolProgress(),
        )
        assert "already_sent" not in response
