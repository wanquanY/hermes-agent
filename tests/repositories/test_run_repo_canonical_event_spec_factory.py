"""``CanonicalEventSpec.from_canonical`` factory —— bridge between the
typed domain ``CanonicalEvent`` (spec §6.2) and the persistence-layer
serialized append spec.

Rationale (docs/v3_audit_report.md §五 #1 revisit): audit flagged
``CanonicalEventSpec.payload: dict[str, Any]`` as a spec §6.2 violation,
but §6.2 forbids ``dict[str, Any]`` on **``CanonicalEvent.payload``**
(the wire event). ``CanonicalEventSpec`` is a distinct persistence-layer
carrier that JSON-serializes to ``run_events.payload_json`` — dict is
correct. The factory here makes the transition from typed → serialized
explicit, so callers holding a typed ``CanonicalEvent`` can flow it in
without hand-serializing.
"""

from __future__ import annotations

from hermes_agent.domain.canonical_event import (
    CanonicalEvent,
    CanonicalEventType,
    ErrorPayload,
    MessageStartPayload,
    ToolCompletePayload,
)
from hermes_agent.repositories.run_repo import CanonicalEventSpec


def test_from_canonical_message_start_serializes_payload_dict():
    event = CanonicalEvent(
        type=CanonicalEventType.MESSAGE_START,
        seq=7,
        session_id="s1",
        run_id="r1",
        turn_id="turn-1",
        timestamp=1_700_000_000,
        payload=MessageStartPayload(role="user", message_id="m1"),
    )
    spec = CanonicalEventSpec.from_canonical(event)
    assert spec.event_type == "message.start"
    assert spec.run_id == "r1"
    assert spec.turn_id == "turn-1"
    assert spec.preassigned_seq == 7
    # payload should contain the typed dataclass fields as a dict
    assert spec.payload["role"] == "user"
    assert spec.payload["message_id"] == "m1"


def test_from_canonical_tool_complete_serializes_status_field():
    event = CanonicalEvent(
        type=CanonicalEventType.TOOL_COMPLETE,
        seq=42,
        session_id="s1",
        run_id="r1",
        turn_id=None,
        timestamp=1_700_000_000,
        payload=ToolCompletePayload(
            tool_call_id="call-1",
            tool_name="grep",
            output="hello",
            status="ok",
        ),
    )
    spec = CanonicalEventSpec.from_canonical(event)
    assert spec.event_type == "tool.complete"
    assert spec.payload["tool_call_id"] == "call-1"
    assert spec.payload["output"] == "hello"
    assert spec.payload["status"] == "ok"


def test_dict_form_still_works_for_low_level_callers():
    """The dict-payload constructor stays functional — persistence spec
    is JSON-oriented, not typed.
    """
    spec = CanonicalEventSpec(
        event_type="message.start",
        payload={"role": "assistant", "message_id": "m1"},
        run_id="r1",
    )
    assert spec.event_type == "message.start"
    assert spec.payload == {"role": "assistant", "message_id": "m1"}


def test_from_canonical_maps_none_turn_id_to_empty_string():
    """CanonicalEvent.turn_id can be None; the spec dict form uses ""."""
    event = CanonicalEvent(
        type=CanonicalEventType.ERROR,
        seq=1,
        session_id="s1",
        run_id="r1",
        turn_id=None,
        timestamp=0,
        payload=ErrorPayload(error_code="5000", message="boom", run_id="r1"),
    )
    spec = CanonicalEventSpec.from_canonical(event)
    assert spec.turn_id == ""
    assert spec.event_type == "error"
    assert spec.payload["error_code"] == "5000"
