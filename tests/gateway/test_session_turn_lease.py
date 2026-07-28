"""Conversation-id turn serialization and Gateway ownership wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_gateway.session_turn_lease import (
    GatewaySessionTurnLeaseService,
    SessionTurnLeaseRegistry,
)


@pytest.mark.asyncio
async def test_alias_routing_keys_are_serialized_in_arrival_order():
    registry = SessionTurnLeaseRegistry()
    events: list[str] = []

    async def turn(owner_key: str, hold: float) -> None:
        token = await registry.acquire(
            "shared-session",
            owner_key=owner_key,
            generation=1,
            timeout=2,
        )
        assert token is not None and not token.degraded
        events.append(f"load:{owner_key}")
        await asyncio.sleep(hold)
        events.append(f"flush:{owner_key}")
        registry.release(token)

    first = asyncio.create_task(turn("route-a", 0.03))
    await asyncio.sleep(0.005)
    second = asyncio.create_task(turn("route-b", 0))
    await asyncio.gather(first, second)

    assert events == [
        "load:route-a",
        "flush:route-a",
        "load:route-b",
        "flush:route-b",
    ]


@pytest.mark.asyncio
async def test_distinct_session_ids_remain_parallel():
    registry = SessionTurnLeaseRegistry()
    starts: list[str] = []
    both_started = asyncio.Event()

    async def turn(session_id: str) -> None:
        token = await registry.acquire(
            session_id,
            owner_key=session_id,
            generation=1,
            timeout=2,
        )
        starts.append(session_id)
        if len(starts) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        registry.release(token)

    await asyncio.gather(turn("session-a"), turn("session-b"))
    assert set(starts) == {"session-a", "session-b"}


@pytest.mark.asyncio
async def test_release_is_generation_scoped_and_idempotent():
    registry = SessionTurnLeaseRegistry()
    stale = await registry.acquire(
        "session-a",
        owner_key="route-a",
        generation=1,
        timeout=2,
    )
    assert registry.release(stale) is True
    assert registry.release(stale) is False

    current = await registry.acquire(
        "session-a",
        owner_key="route-a",
        generation=2,
        timeout=2,
    )
    assert current is not None
    assert stale is not None
    stale.released = False
    assert registry.release(stale) is False

    waiter = asyncio.create_task(
        registry.acquire(
            "session-a",
            owner_key="route-b",
            generation=1,
            timeout=2,
        )
    )
    await asyncio.sleep(0.01)
    assert not waiter.done()
    assert registry.release(current) is True
    next_token = await waiter
    assert next_token is not None and not next_token.degraded
    registry.release(next_token)


@pytest.mark.asyncio
async def test_timeout_fails_open_without_stealing_holder(caplog):
    registry = SessionTurnLeaseRegistry()
    holder = await registry.acquire(
        "session-a",
        owner_key="route-a",
        generation=1,
        timeout=2,
    )

    with caplog.at_level("ERROR", logger="hermes_gateway.session_turn_lease"):
        degraded = await registry.acquire(
            "session-a",
            owner_key="route-b",
            generation=1,
            timeout=0.01,
        )

    assert degraded is not None and degraded.degraded
    assert registry.release(degraded) is False
    assert any("failing open" in message for message in caplog.messages)

    waiter = asyncio.create_task(
        registry.acquire(
            "session-a",
            owner_key="route-c",
            generation=1,
            timeout=2,
        )
    )
    await asyncio.sleep(0.01)
    assert not waiter.done()
    registry.release(holder)
    token = await waiter
    registry.release(token)


@pytest.mark.asyncio
async def test_registry_is_bounded_without_evicting_live_lease():
    registry = SessionTurnLeaseRegistry(max_entries=4)
    live = await registry.acquire(
        "live-session",
        owner_key="route-a",
        generation=1,
        timeout=2,
    )
    for index in range(30):
        token = await registry.acquire(
            f"idle-{index}",
            owner_key="churn",
            generation=index,
            timeout=2,
        )
        registry.release(token)

    assert len(registry) <= 5
    assert registry.release(live) is True


@pytest.mark.asyncio
async def test_rebind_makes_lease_follow_compression_rotation():
    registry = SessionTurnLeaseRegistry()
    holder = await registry.acquire(
        "parent",
        owner_key="route-a",
        generation=1,
        timeout=2,
    )
    assert registry.rebind(holder, "child") is True
    assert holder is not None and holder.session_id == "child"

    waiter = asyncio.create_task(
        registry.acquire(
            "child",
            owner_key="route-b",
            generation=1,
            timeout=2,
        )
    )
    await asyncio.sleep(0.01)
    assert not waiter.done()
    registry.release(holder)
    token = await waiter
    registry.release(token)


@pytest.mark.asyncio
async def test_service_owns_tokens_by_routing_key_and_generation(monkeypatch):
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "2")
    service = GatewaySessionTurnLeaseService(SimpleNamespace())
    await service.acquire("session-a", routing_key="route-a", generation=3)

    assert service.release("route-a", 2) is False
    assert service.rebind("route-a", 3, "session-b") is True
    assert service.release("route-a", 3) is True
    assert service.release("route-a", 3) is False


def test_registry_empty_session_and_service_empty_key_are_safe():
    async def scenario() -> None:
        registry = SessionTurnLeaseRegistry()
        assert (
            await registry.acquire("", owner_key="route", generation=1)
            is None
        )
        assert registry.release(None) is False

    asyncio.run(scenario())
    service = GatewaySessionTurnLeaseService(SimpleNamespace())
    assert service.release("", 1) is False
