from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from hermes_agent.composition.async_sqlite import shutdown_async_sqlite_boundary
from hermes_gateway.title_command import title_command_for


@pytest.mark.asyncio
async def test_slow_gateway_session_lookup_keeps_event_loop_responsive() -> None:
    started = threading.Event()
    release = threading.Event()
    owner_thread: list[int] = []

    class SlowSessionStore:
        def get_or_create_session(self, _source):
            owner_thread.append(threading.get_ident())
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(session_id="session-slow-db")

    runner = SimpleNamespace(
        session_store=SlowSessionStore(),
        _session_db=None,
        _format_session_db_unavailable=lambda: "unavailable",
    )
    source = SimpleNamespace(platform=None, user_id="user-1")
    event = SimpleNamespace(source=source, get_command_args=lambda: "")
    loop_thread = threading.get_ident()

    task = asyncio.create_task(title_command_for(runner).handle_title_command(event))
    while not started.is_set():
        await asyncio.sleep(0.001)

    ticks = 0
    for _ in range(20):
        ticks += 1
        await asyncio.sleep(0.002)
    assert ticks == 20
    assert not task.done()

    release.set()
    assert await task == "unavailable"
    assert owner_thread and owner_thread[0] != loop_thread
    await shutdown_async_sqlite_boundary()
