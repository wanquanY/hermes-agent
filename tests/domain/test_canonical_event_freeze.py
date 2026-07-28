"""spec §6.2 CanonicalEvent 14-arm freeze —— 系统性契约锁.

Phase A freeze (spec §6.2 line 532): 后加 arm / 改字段类型 / 删字段都不允许，
只能 additive。前端 Phase A5 exhaustive switch + never 依赖此锁。
"""

from __future__ import annotations

from dataclasses import fields
from typing import get_args, get_type_hints

import pytest

from hermes_agent.domain.canonical_event import (
    CANONICAL_EVENT_ARM_COUNT,
    CanonicalEvent,
    CanonicalEventType,
    ErrorPayload,
    MessageCompletePayload,
    MessageDeltaPayload,
    MessageStartPayload,
    OriginatingRef,
    PAYLOAD_BY_TYPE,
    ReasoningAvailablePayload,
    ReasoningDeltaPayload,
    SessionInterruptedPayload,
    SessionRecalledPayload,
    ThinkingDeltaPayload,
    ToolCompletePayload,
    ToolDeltaPayload,
    ToolGeneratingPayload,
    ToolProgressPayload,
    ToolStartPayload,
    TypedPayload,
)


# ---------------------------------------------------------------------------
# Enum 14 arm freeze
# ---------------------------------------------------------------------------


def test_canonical_event_type_has_exactly_14_arms():
    assert len(list(CanonicalEventType)) == CANONICAL_EVENT_ARM_COUNT
    assert CANONICAL_EVENT_ARM_COUNT == 14


def test_canonical_event_type_values_are_exact():
    """Wire strings are the contract with the frontend v3.1 union."""
    expected = {
        "message.start",
        "message.delta",
        "message.complete",
        "reasoning.delta",
        "reasoning.available",
        "thinking.delta",
        "tool.start",
        "tool.generating",
        "tool.progress",
        "tool.delta",
        "tool.complete",
        "error",
        "session.interrupted",
        "session.recalled",
    }
    assert {m.value for m in CanonicalEventType} == expected


def test_interaction_events_are_not_in_canonical_enum():
    """spec §6.2 line 405-407: interaction.* is NOT canonical (§7.4 wire channel)."""
    for member in CanonicalEventType:
        assert not member.value.startswith("interaction."), (
            f"interaction.* leaked into CanonicalEventType: {member.value!r}"
        )


# ---------------------------------------------------------------------------
# TypedPayload union covers 14 payload dataclasses
# ---------------------------------------------------------------------------


def test_typed_payload_union_has_14_members():
    args = get_args(TypedPayload)
    assert len(args) == 14


def test_payload_by_type_covers_every_arm():
    for member in CanonicalEventType:
        assert member in PAYLOAD_BY_TYPE, (
            f"{member.name} missing from PAYLOAD_BY_TYPE"
        )


def test_payload_dataclasses_are_frozen():
    """spec §6.2 requires immutability so wire hashes stay stable."""
    for _, payload_cls in PAYLOAD_BY_TYPE.items():
        # frozen dataclass -> ``__dataclass_params__.frozen`` True
        params = getattr(payload_cls, "__dataclass_params__", None)
        assert params is not None, f"{payload_cls.__name__} is not a dataclass"
        assert params.frozen, f"{payload_cls.__name__} must be frozen"


# ---------------------------------------------------------------------------
# Payload field freeze per spec §6.2.1
# ---------------------------------------------------------------------------


def _field_names(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def test_message_start_payload_fields():
    assert _field_names(MessageStartPayload) == {
        "role",
        "message_id",
        "client_message_id",
        "originating",
        "participant_id",
        "channel",
        "visibility",
    }


def test_originating_ref_fields():
    assert _field_names(OriginatingRef) == {"client_message_id", "user_message_id"}


def test_message_delta_payload_fields():
    assert _field_names(MessageDeltaPayload) == {"text", "message_id"}


def test_message_complete_payload_fields():
    assert _field_names(MessageCompletePayload) == {"message_id", "status"}


def test_reasoning_delta_payload_fields():
    assert _field_names(ReasoningDeltaPayload) == {"text", "run_id", "turn_id"}


def test_reasoning_available_payload_fields():
    assert _field_names(ReasoningAvailablePayload) == {"text", "run_id", "turn_id"}


def test_thinking_delta_payload_fields():
    assert _field_names(ThinkingDeltaPayload) == {"text", "run_id", "turn_id"}


def test_tool_start_payload_fields():
    assert _field_names(ToolStartPayload) == {
        "tool_call_id",
        "tool_name",
        "tool_target",
        "arguments",
    }


def test_tool_generating_payload_fields():
    assert _field_names(ToolGeneratingPayload) == {
        "tool_call_id",
        "tool_name",
        "tool_target",
        "text_chunk",
    }


def test_tool_progress_payload_fields():
    assert _field_names(ToolProgressPayload) == {"tool_call_id", "progress_text"}


def test_tool_delta_payload_fields():
    assert _field_names(ToolDeltaPayload) == {"tool_call_id", "output_chunk"}


def test_tool_complete_payload_fields():
    assert _field_names(ToolCompletePayload) == {
        "tool_call_id",
        "tool_name",
        "output",
        "status",
        "error_text",
    }


def test_error_payload_fields():
    assert _field_names(ErrorPayload) == {"error_code", "message", "run_id"}


def test_session_interrupted_payload_fields():
    assert _field_names(SessionInterruptedPayload) == {"run_id", "reason"}


def test_session_recalled_payload_fields():
    assert _field_names(SessionRecalledPayload) == {"from_seq"}


# ---------------------------------------------------------------------------
# CanonicalEvent construction invariants
# ---------------------------------------------------------------------------


def test_canonical_event_accepts_matching_payload():
    ev = CanonicalEvent(
        type=CanonicalEventType.MESSAGE_START,
        seq=1,
        session_id="s1",
        run_id="r1",
        turn_id="t1",
        timestamp=1_700_000_000,
        payload=MessageStartPayload(role="user"),
    )
    assert ev.type is CanonicalEventType.MESSAGE_START
    assert ev.payload.role == "user"


def test_canonical_event_rejects_mismatched_payload():
    with pytest.raises(TypeError, match="requires payload of type MessageStartPayload"):
        CanonicalEvent(
            type=CanonicalEventType.MESSAGE_START,
            seq=1,
            session_id="s1",
            run_id="r1",
            turn_id=None,
            timestamp=1,
            payload=MessageDeltaPayload(text="hi", message_id="m1"),  # wrong type
        )


def test_canonical_event_rejects_empty_session_id():
    with pytest.raises(ValueError, match="session_id is required"):
        CanonicalEvent(
            type=CanonicalEventType.MESSAGE_START,
            seq=1,
            session_id="",
            run_id="r1",
            turn_id=None,
            timestamp=1,
            payload=MessageStartPayload(role="user"),
        )


def test_canonical_event_rejects_unknown_run_id():
    with pytest.raises(ValueError, match="run_id must be non-empty and not 'unknown'"):
        CanonicalEvent(
            type=CanonicalEventType.MESSAGE_START,
            seq=1,
            session_id="s1",
            run_id="unknown",
            turn_id=None,
            timestamp=1,
            payload=MessageStartPayload(role="user"),
        )


def test_canonical_event_rejects_empty_run_id():
    with pytest.raises(ValueError, match="run_id must be non-empty"):
        CanonicalEvent(
            type=CanonicalEventType.MESSAGE_START,
            seq=1,
            session_id="s1",
            run_id="",
            turn_id=None,
            timestamp=1,
            payload=MessageStartPayload(role="user"),
        )


# ---------------------------------------------------------------------------
# Anti-drift: payload MUST NOT be `dict[str, Any]` on CanonicalEvent
# ---------------------------------------------------------------------------


def test_canonical_event_payload_annotation_is_typed_payload_not_dict():
    """spec §6.2 line 419: `payload: TypedPayload  # 14 arm 具体 payload
    dataclass 之一，无 Dict[str, Any]`.

    A regression here — someone widening `payload` to `dict[str, Any]` —
    is exactly what the audit report §五 #1 called out.
    """
    hints = get_type_hints(CanonicalEvent)
    payload_type = hints["payload"]
    # `TypedPayload` is a Union; hints resolves it to the same object
    assert payload_type is TypedPayload, (
        f"CanonicalEvent.payload annotation drift — expected TypedPayload, "
        f"got {payload_type!r}"
    )


def test_no_payload_dataclass_uses_dict_str_any_for_a_typed_field():
    """spec §6.2 disallow ``dict[str, Any]`` on payload dataclasses except
    where the spec explicitly permits it (ToolStartPayload.arguments —
    tool argument bag, opaque by design).
    """
    exempt_fields = {
        (ToolStartPayload.__name__, "arguments"),
    }
    for payload_cls in PAYLOAD_BY_TYPE.values():
        hints = get_type_hints(payload_cls)
        for name, hint in hints.items():
            hint_str = str(hint)
            if (payload_cls.__name__, name) in exempt_fields:
                continue
            assert "dict[str, typing.Any]" not in hint_str.lower().replace(" ", ""), (
                f"{payload_cls.__name__}.{name}: {hint_str!r} — spec §6.2 forbids "
                f"dict[str, Any] on typed payloads (add to exempt_fields with "
                f"a spec citation if truly opaque)"
            )
