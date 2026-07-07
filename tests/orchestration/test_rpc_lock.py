"""Phase F — ShardedRpcLock per-session concurrency (spec §8.2)."""

from __future__ import annotations

import asyncio

import pytest

from hermes_agent.orchestration import (
    LockLease,
    RpcBusy,
    ShardedRpcLock,
)


@pytest.mark.asyncio
async def test_acquire_returns_lease_context_manager():
    lock_pool = ShardedRpcLock()
    async with await lock_pool.acquire("s1") as lease:
        assert isinstance(lease, LockLease)
        assert lease.session_id == "s1"


@pytest.mark.asyncio
async def test_same_session_second_acquire_blocks_until_release():
    lock_pool = ShardedRpcLock()
    first = await lock_pool.acquire("s1", timeout=1.0)
    # Attempt a second acquire — should time out.
    with pytest.raises(RpcBusy):
        await lock_pool.acquire("s1", timeout=0.1)
    first.release()

    # Now it should acquire immediately.
    second = await lock_pool.acquire("s1", timeout=1.0)
    second.release()


@pytest.mark.asyncio
async def test_different_sessions_do_not_contend():
    """spec §8.2 — cross-session traffic must NOT serialize."""
    lock_pool = ShardedRpcLock()
    lease_a = await lock_pool.acquire("s1", timeout=1.0)
    lease_b = await lock_pool.acquire("s2", timeout=1.0)  # must not block on s1
    assert lease_a.session_id == "s1"
    assert lease_b.session_id == "s2"
    lease_a.release()
    lease_b.release()


@pytest.mark.asyncio
async def test_timeout_raises_rpc_busy():
    lock_pool = ShardedRpcLock()
    await lock_pool.acquire("s1", timeout=1.0)  # never released
    with pytest.raises(RpcBusy, match="ShardedRpcLock busy"):
        await lock_pool.acquire("s1", timeout=0.05)


@pytest.mark.asyncio
async def test_double_release_is_idempotent():
    lock_pool = ShardedRpcLock()
    lease = await lock_pool.acquire("s1")
    lease.release()
    lease.release()  # no raise; logs a warning only


@pytest.mark.asyncio
async def test_empty_session_id_rejected():
    lock_pool = ShardedRpcLock()
    with pytest.raises(ValueError):
        await lock_pool.acquire("")


@pytest.mark.asyncio
async def test_peek_lease_and_active_sessions():
    lock_pool = ShardedRpcLock()
    lease = await lock_pool.acquire("s1")
    assert lock_pool.peek_lease("s1") is lease
    assert lock_pool.active_session_ids() == ["s1"]
    lease.release()
