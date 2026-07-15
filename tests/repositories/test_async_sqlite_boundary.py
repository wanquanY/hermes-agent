from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from hermes_agent.composition.async_sqlite import (
    AsyncSQLiteBoundary,
    AsyncSQLiteBoundaryClosed,
)


@pytest.mark.asyncio
async def test_slow_real_sqlite_operation_does_not_block_event_loop(tmp_path) -> None:
    boundary = AsyncSQLiteBoundary(thread_name_prefix="test-sqlite-heartbeat")
    state: dict[str, object] = {}
    started = threading.Event()
    release = threading.Event()

    def initialize() -> None:
        conn = sqlite3.connect(tmp_path / "async.db", isolation_level=None)
        conn.execute("CREATE TABLE events (value TEXT NOT NULL)")
        state["conn"] = conn
        state["owner_thread"] = threading.get_ident()

    def slow_write() -> int:
        conn = state["conn"]
        assert isinstance(conn, sqlite3.Connection)
        assert threading.get_ident() == state["owner_thread"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO events VALUES ('persisted')")
        started.set()
        release.wait(timeout=2)
        conn.execute("COMMIT")
        return threading.get_ident()

    await boundary.run(initialize)
    task = asyncio.create_task(boundary.run(slow_write))
    while not started.is_set():
        await asyncio.sleep(0.001)

    ticks = 0
    for _ in range(20):
        ticks += 1
        await asyncio.sleep(0.002)
    assert ticks == 20
    assert not task.done()

    release.set()
    assert await task == state["owner_thread"]

    def verify_and_close() -> list[tuple[str]]:
        conn = state["conn"]
        assert isinstance(conn, sqlite3.Connection)
        rows = conn.execute("SELECT value FROM events").fetchall()
        conn.close()
        return rows

    assert await boundary.run(verify_and_close) == [("persisted",)]
    await boundary.shutdown()


@pytest.mark.asyncio
async def test_cancellation_does_not_interrupt_inflight_transaction(tmp_path) -> None:
    boundary = AsyncSQLiteBoundary(thread_name_prefix="test-sqlite-cancel")
    state: dict[str, sqlite3.Connection] = {}
    started = threading.Event()
    release = threading.Event()

    def write_transaction() -> None:
        conn = sqlite3.connect(tmp_path / "cancel.db", isolation_level=None)
        state["conn"] = conn
        conn.execute("CREATE TABLE events (value TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO events VALUES ('committed-after-cancel')")
        started.set()
        release.wait(timeout=2)
        conn.execute("COMMIT")

    task = asyncio.create_task(boundary.run(write_transaction))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()

    def verify_and_close() -> str:
        conn = state["conn"]
        value = conn.execute("SELECT value FROM events").fetchone()[0]
        conn.close()
        return value

    assert await boundary.run(verify_and_close) == "committed-after-cancel"
    await boundary.shutdown()


@pytest.mark.asyncio
async def test_timeout_keeps_order_and_shutdown_rejects_new_work() -> None:
    boundary = AsyncSQLiteBoundary(thread_name_prefix="test-sqlite-timeout")
    release = threading.Event()
    order: list[str] = []

    def slow() -> None:
        order.append("slow-start")
        release.wait(timeout=2)
        order.append("slow-finish")

    with pytest.raises(TimeoutError):
        await boundary.run(slow, timeout=0.01)
    release.set()
    await boundary.run(lambda: order.append("next"))
    assert order == ["slow-start", "slow-finish", "next"]

    await boundary.shutdown()
    with pytest.raises(AsyncSQLiteBoundaryClosed):
        await boundary.run(lambda: None)


@pytest.mark.asyncio
async def test_concurrent_shutdown_callers_wait_for_accepted_work() -> None:
    boundary = AsyncSQLiteBoundary(thread_name_prefix="test-sqlite-shutdown")
    started = threading.Event()
    release = threading.Event()

    def accepted_work() -> None:
        started.set()
        release.wait(timeout=2)

    work = asyncio.create_task(boundary.run(accepted_work))
    while not started.is_set():
        await asyncio.sleep(0.001)

    first_shutdown = asyncio.create_task(boundary.shutdown())
    await asyncio.sleep(0)
    second_shutdown = asyncio.create_task(boundary.shutdown())
    await asyncio.sleep(0.01)

    with pytest.raises(AsyncSQLiteBoundaryClosed):
        await boundary.run(lambda: None)
    assert not first_shutdown.done()
    assert not second_shutdown.done()

    release.set()
    await work
    await asyncio.gather(first_shutdown, second_shutdown)
