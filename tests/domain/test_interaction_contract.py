"""spec §7.4 Interaction 双通道类型契约验证.

Covers the four types the audit (docs/v3_audit_report.md §五 #2) flagged
as "全部未建" plus the ``anchor_seq`` semantic fix (§四 伪绿 3).
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from typing import get_args, get_type_hints

import pytest

from hermes_agent.domain.canonical_event import CanonicalEventType
from hermes_agent.domain.interaction import (
    InteractionFrame,
    InteractionFrameType,
    InteractionKind,
    InteractionRegistry,
    InteractionRequest,
    InteractionResponse,
    InternalRunEventType,
)


# ---------------------------------------------------------------------------
# §7.4.2 InternalRunEventType (persistence channel)
# ---------------------------------------------------------------------------


def test_internal_run_event_type_has_exact_three_arms():
    assert set(m.value for m in InternalRunEventType) == {
        "_internal.interaction.requested",
        "_internal.interaction.resolved",
        "_internal.interaction.expired",
    }


def test_internal_run_event_type_uses_underscore_prefix_convention():
    """spec §7.4.2 line 767-773 —— ``_internal.`` prefix keeps these events
    out of the wire path (EventLedger.list filters them by default).
    """
    for member in InternalRunEventType:
        assert member.value.startswith("_internal."), (
            f"{member.name} lost the _internal. prefix: {member.value!r}"
        )


def test_internal_run_event_type_disjoint_from_canonical_event_type():
    """spec §6.2 line 405-407 —— interaction.* MUST NOT collide with
    the canonical enum values.
    """
    internal_values = {m.value for m in InternalRunEventType}
    canonical_values = {m.value for m in CanonicalEventType}
    assert internal_values.isdisjoint(canonical_values)


# ---------------------------------------------------------------------------
# §7.4.3 InteractionFrameType + InteractionFrame (delivery channel)
# ---------------------------------------------------------------------------


def test_interaction_frame_type_has_exact_three_arms():
    assert set(m.value for m in InteractionFrameType) == {
        "interaction.requested",
        "interaction.resolved",
        "interaction.expired",
    }


def test_interaction_frame_type_does_not_use_internal_prefix():
    """Wire frame types must NOT carry the ``_internal.`` prefix — that
    prefix is reserved for the persistence channel (§7.4.2).
    """
    for member in InteractionFrameType:
        assert not member.value.startswith("_internal.")


def test_interaction_frame_type_disjoint_from_canonical_event_type():
    """spec §6.2 line 386 —— InteractionFrame is a **separate frame type**,
    not a canonical event. Values must not collide.
    """
    frame_values = {m.value for m in InteractionFrameType}
    canonical_values = {m.value for m in CanonicalEventType}
    assert frame_values.isdisjoint(canonical_values)


def test_interaction_frame_field_shape():
    """spec §7.4.3 line 785-793 field-by-field lock."""
    assert {f.name for f in fields(InteractionFrame)} == {
        "type",
        "request_id",
        "kind",
        "run_id",
        "turn_id",
        "anchor_seq",
        "payload",
        "timestamp",
    }


def test_interaction_frame_is_frozen():
    params = getattr(InteractionFrame, "__dataclass_params__", None)
    assert params is not None
    assert params.frozen


def test_interaction_frame_constructs_ok():
    frame = InteractionFrame(
        type=InteractionFrameType.REQUESTED,
        request_id="req-1",
        kind="approval",
        run_id="run-1",
        turn_id="t-1",
        anchor_seq=42,
        payload={"foo": "bar"},
        timestamp=1_700_000_000,
    )
    assert frame.type is InteractionFrameType.REQUESTED
    assert frame.anchor_seq == 42


def test_interaction_frame_rejects_empty_request_id():
    with pytest.raises(ValueError, match="request_id is required"):
        InteractionFrame(
            type=InteractionFrameType.REQUESTED,
            request_id="",
            kind="approval",
            run_id="r",
            turn_id=None,
            anchor_seq=0,
            payload={},
            timestamp=0,
        )


def test_interaction_frame_rejects_negative_anchor_seq():
    with pytest.raises(ValueError, match="anchor_seq must be >= 0"):
        InteractionFrame(
            type=InteractionFrameType.REQUESTED,
            request_id="req-1",
            kind="approval",
            run_id="r",
            turn_id=None,
            anchor_seq=-1,
            payload={},
            timestamp=0,
        )


# ---------------------------------------------------------------------------
# §7.4.4 InteractionRegistry Protocol — anchor_seq REQUIRED kwonly
# ---------------------------------------------------------------------------


def test_interaction_registry_request_has_anchor_seq_as_required_kwonly():
    """spec §7.4.4 line 808 + 834:
    ``anchor_seq`` is a required keyword-only argument. If missed at the
    call site, mypy / runtime should complain; this test uses inspect to
    prove the signature.
    """
    sig = inspect.signature(InteractionRegistry.request)
    params = sig.parameters
    assert "anchor_seq" in params, "anchor_seq missing from request signature"
    p = params["anchor_seq"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"anchor_seq must be KEYWORD_ONLY, got {p.kind}"
    )
    assert p.default is inspect.Parameter.empty, (
        f"anchor_seq must have NO default (required), got {p.default!r}"
    )


def test_interaction_registry_protocol_has_expected_methods():
    """spec §7.4.4 line 801-832 — the four methods on the registry."""
    expected_methods = {"request", "resolve", "expire", "list_pending"}
    actual = {
        m for m in dir(InteractionRegistry)
        if not m.startswith("_") and callable(getattr(InteractionRegistry, m))
    }
    assert expected_methods.issubset(actual), (
        f"InteractionRegistry protocol missing methods: {expected_methods - actual}"
    )


def test_interaction_registry_is_runtime_checkable():
    """Callers can use ``isinstance(x, InteractionRegistry)`` for wiring
    assertions.
    """

    class MinimalImpl:
        def request(self, run_id, kind, payload, *, anchor_seq):
            return InteractionRequest(
                request_id="r-1",
                run_id=run_id,
                kind=kind,
                anchor_seq=anchor_seq,
                payload=payload,
                timestamp=0,
            )

        def resolve(self, request_id, response):
            return None

        def expire(self, request_id):
            return None

        def list_pending(self, session_id):
            return []

    inst = MinimalImpl()
    assert isinstance(inst, InteractionRegistry)


# ---------------------------------------------------------------------------
# InteractionRequest / InteractionResponse dataclass locks
# ---------------------------------------------------------------------------


def test_interaction_request_carries_anchor_seq_field():
    """spec §7.4.4 line 815 — the returned handle carries ``anchor_seq``
    so callers can thread it forward without re-derivation.
    """
    assert "anchor_seq" in {f.name for f in fields(InteractionRequest)}


def test_interaction_response_shape():
    assert {f.name for f in fields(InteractionResponse)} == {
        "request_id",
        "response_payload",
    }
