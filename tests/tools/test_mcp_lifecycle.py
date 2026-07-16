import asyncio

import pytest

from tools.mcp_lifecycle import (
    MCPInitializeTimeout,
    MCPParkPolicy,
    bounded_initialize,
)


class _Session:
    def __init__(self, gate: asyncio.Event | None = None):
        self.gate = gate
        self.started = asyncio.Event()
        self.cancelled = False

    async def initialize(self):
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            return {"ok": True}
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_bounded_initialize_returns_success():
    assert await bounded_initialize(
        _Session(), timeout=0.5, server_name="ok", transport="stdio"
    ) == {"ok": True}


@pytest.mark.asyncio
async def test_bounded_initialize_cancels_and_drains_hung_handshake():
    session = _Session(asyncio.Event())
    with pytest.raises(MCPInitializeTimeout, match="hung.*stdio.*timed out"):
        await bounded_initialize(
            session, timeout=0.01, server_name="hung", transport="stdio"
        )
    assert session.cancelled is True
    assert not [
        task for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task.get_name().startswith("mcp-initialize:hung")
    ]


@pytest.mark.asyncio
async def test_caller_cancellation_drains_initialize_child():
    session = _Session(asyncio.Event())
    owner = asyncio.create_task(
        bounded_initialize(
            session, timeout=30, server_name="cancel", transport="http"
        )
    )
    await session.started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert session.cancelled is True


def test_park_policy_is_bounded_exponential_backoff():
    policy = MCPParkPolicy(initial_delay=2, maximum_delay=10, multiplier=2)
    assert [policy.delay_for(i) for i in range(1, 6)] == [2, 4, 8, 10, 10]
