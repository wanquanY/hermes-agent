"""Phase K — property-style tests for TransportRouter (spec §3 L5)."""

from __future__ import annotations

import random

from hermes_agent.gateway import (
    AllowAllResolver,
    MethodRegistry,
    requires_permission,
)
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.transport import (
    InMemoryTransport,
    TransportRouter,
)
from hermes_agent.transport.envelope import FrameKind


PROPERTY_ROUNDS = 24


@requires_permission("test.read", read_only=True)
def _echo_handler(params, ctx: DispatchContext):
    return {"echoed": params, "method": ctx.method}


@requires_permission("test.read", read_only=True)
def _identity_handler(params, ctx: DispatchContext):
    return {"id_in_result": ctx.request_id}


@requires_permission("test.write")
def _boom_handler(params, ctx: DispatchContext):
    raise RuntimeError("kaboom")


def _wired():
    reg = MethodRegistry()
    reg.register("echo", _echo_handler)
    reg.register("identity", _identity_handler)
    reg.register("boom", _boom_handler)
    transport = InMemoryTransport()
    router = TransportRouter(
        transport=transport,
        registry=reg,
        resolver=AllowAllResolver(),
    )
    return transport, router


def test_property_every_request_receives_exactly_one_response_frame():
    """N random requests → N outbound frames (either RESPONSE or ERROR)."""
    rng = random.Random(20260801)
    for round_idx in range(PROPERTY_ROUNDS):
        transport, router = _wired()
        router.start()
        transport.pop_sent()

        n = rng.randint(1, 30)
        for i in range(n):
            method = rng.choice(("echo", "identity", "boom", "no.such"))
            transport.client_send(
                {"id": f"req-{round_idx}-{i}", "method": method, "params": {"i": i}}
            )
        processed = router.pump_until_idle()
        assert processed == n

        sent = transport.pop_sent()
        assert len(sent) == n, (
            f"round {round_idx}: expected {n} outbound frames, got {len(sent)}"
        )
        for frame in sent:
            assert frame.kind in {FrameKind.RESPONSE, FrameKind.ERROR}


def test_property_request_id_preserved_across_any_method():
    rng = random.Random(20260802)
    for round_idx in range(PROPERTY_ROUNDS):
        transport, router = _wired()
        router.start()
        transport.pop_sent()

        sent_ids: list[str] = []
        n = rng.randint(1, 20)
        for i in range(n):
            rid = f"req-{round_idx}-{i}-{rng.randint(0, 999)}"
            sent_ids.append(rid)
            transport.client_send(
                {"id": rid, "method": rng.choice(("echo", "no.such")), "params": {}}
            )
        router.pump_until_idle()

        recv_ids = [frame.id for frame in transport.pop_sent()]
        # Order-preserving.
        assert recv_ids == sent_ids


def test_property_router_stats_track_every_frame():
    rng = random.Random(20260803)
    for round_idx in range(PROPERTY_ROUNDS):
        transport, router = _wired()
        router.start()
        transport.pop_sent()

        good = rng.randint(0, 12)
        errors = rng.randint(0, 12)
        for i in range(good):
            transport.client_send(
                {"id": f"g-{i}", "method": "echo", "params": {"i": i}}
            )
        for i in range(errors):
            transport.client_send({"id": f"b-{i}", "method": "no.such"})
        router.pump_until_idle()

        stats = router.stats()
        assert stats.handshakes_sent == 1
        assert stats.requests_dispatched == good
        assert stats.errors_returned == errors


def test_property_random_backlog_size_pump_until_idle_matches():
    rng = random.Random(20260804)
    for round_idx in range(PROPERTY_ROUNDS):
        transport, router = _wired()
        router.start()
        transport.pop_sent()

        n = rng.randint(0, 40)
        for i in range(n):
            transport.client_send(
                {"id": f"r-{i}", "method": "echo", "params": {}}
            )
        processed = router.pump_until_idle()
        assert processed == n


def test_property_error_frame_carries_matching_id():
    """Even error frames must echo the request id back so the client can pair."""
    rng = random.Random(20260805)
    for round_idx in range(PROPERTY_ROUNDS):
        transport, router = _wired()
        router.start()
        transport.pop_sent()

        for i in range(rng.randint(1, 10)):
            rid = f"err-{round_idx}-{i}"
            transport.client_send({"id": rid, "method": "no.such"})
        router.pump_until_idle()
        for frame in transport.pop_sent():
            assert frame.kind is FrameKind.ERROR
            assert frame.id.startswith(f"err-{round_idx}-")
