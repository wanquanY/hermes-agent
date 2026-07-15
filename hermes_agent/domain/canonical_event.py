"""spec §6.2 CanonicalEvent 14-arm freeze.

Backend canonical event schema **mirrors the frontend v3.1 CanonicalEvent
union field-by-field**. This module is the sole authority for:

- ``CanonicalEventType`` enum (14 arms; interaction.* is NOT here — see §7.4)
- 14 payload dataclasses, one per arm, frozen and typed strictly
- ``TypedPayload`` union covering the 14 payloads
- ``CanonicalEvent`` dataclass with ``payload: TypedPayload`` (**no ``dict[str, Any]``**)

The 14 arms are:
    message.start / message.delta / message.complete
    reasoning.delta / reasoning.available / thinking.delta
    tool.start / tool.generating / tool.progress / tool.delta / tool.complete
    error / session.interrupted / session.recalled

Spec §6.2 explicit constraint: **Phase A freeze**. After Phase A, payload
fields may only be additive — never rename / remove / retype. Frontend
Phase A5 ``exhaustive switch + never`` compiles a union closed on these
14 arms.

Interaction events are NOT canonical — they live in ``InteractionFrame``
and ``InternalRunEventType._internal.interaction.*`` per spec §7.4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union


# ---------------------------------------------------------------------------
# CanonicalEventType — spec §6.2 line 389-408
# ---------------------------------------------------------------------------


class CanonicalEventType(str, Enum):
    """The 14 canonical event arms (spec §6.2 P0-B1)."""

    MESSAGE_START = "message.start"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETE = "message.complete"
    REASONING_DELTA = "reasoning.delta"
    REASONING_AVAILABLE = "reasoning.available"
    THINKING_DELTA = "thinking.delta"       # v3.0.2 added
    TOOL_START = "tool.start"
    TOOL_GENERATING = "tool.generating"     # v3.0.2 added
    TOOL_PROGRESS = "tool.progress"
    TOOL_DELTA = "tool.delta"               # v3.0.2 added
    TOOL_COMPLETE = "tool.complete"
    ERROR = "error"
    SESSION_INTERRUPTED = "session.interrupted"
    SESSION_RECALLED = "session.recalled"


# Guarantee (locked by tests): interaction.* is not a canonical arm.
# ``_internal.interaction.*`` persists via EventLedger for crash recovery.
# ``interaction.*`` wire frame lives in InteractionFrame (§7.4).


# ---------------------------------------------------------------------------
# 14 payload dataclasses — spec §6.2.1 line 422-530
# ---------------------------------------------------------------------------


# --- Message lifecycle (3 arms) ---


@dataclass(frozen=True)
class OriginatingRef:
    """Referenced by ``MessageStartPayload.originating``."""

    client_message_id: str | None = None
    user_message_id: str | None = None


@dataclass(frozen=True)
class MessageStartPayload:
    role: Literal["assistant", "user", "tool", "system"]
    message_id: str | None = None
    client_message_id: str | None = None
    originating: OriginatingRef | None = None
    participant_id: str | None = None
    channel: Literal["visible", "handoff", "system"] | None = None
    visibility: Literal["user", "team", "handoff", "internal"] | None = None


@dataclass(frozen=True)
class MessageDeltaPayload:
    text: str
    message_id: str


@dataclass(frozen=True)
class MessageCompletePayload:
    message_id: str
    # spec §6.2.1 line 449-453: `failed` reserved for ERROR arm.
    status: Literal["completed", "interrupted", "cancelled"]


# --- Reasoning / Thinking (3 arms) ---


@dataclass(frozen=True)
class ReasoningDeltaPayload:
    text: str
    run_id: str
    turn_id: str | None = None


@dataclass(frozen=True)
class ReasoningAvailablePayload:
    text: str
    run_id: str
    turn_id: str | None = None


@dataclass(frozen=True)
class ThinkingDeltaPayload:
    """v3.0.2 added (spec §6.2.1)."""

    text: str
    run_id: str
    turn_id: str | None = None


# --- Tool lifecycle (5 arms) ---


@dataclass(frozen=True)
class ToolStartPayload:
    tool_call_id: str
    tool_name: str
    tool_target: str | None = None
    arguments: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolGeneratingPayload:
    """v3.0.2 added — LLM streaming a tool_call payload."""

    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_target: str | None = None
    text_chunk: str | None = None


@dataclass(frozen=True)
class ToolProgressPayload:
    tool_call_id: str
    progress_text: str


@dataclass(frozen=True)
class ToolDeltaPayload:
    """v3.0.2 added — tool's runtime output streaming."""

    tool_call_id: str
    output_chunk: str


@dataclass(frozen=True)
class ToolCompletePayload:
    tool_call_id: str
    tool_name: str
    output: str
    status: Literal["ok", "failed"] = "ok"
    error_text: str | None = None


# --- Session lifecycle / error (3 arms) ---


@dataclass(frozen=True)
class ErrorPayload:
    error_code: str  # from spec §J9 ErrorCode enum
    message: str
    run_id: str | None = None


@dataclass(frozen=True)
class SessionInterruptedPayload:
    run_id: str
    reason: str


@dataclass(frozen=True)
class SessionRecalledPayload:
    from_seq: int


# ---------------------------------------------------------------------------
# TypedPayload union + CanonicalEvent — spec §6.2 line 410-420, 523-529
# ---------------------------------------------------------------------------


TypedPayload = Union[
    MessageStartPayload,
    MessageDeltaPayload,
    MessageCompletePayload,
    ReasoningDeltaPayload,
    ReasoningAvailablePayload,
    ThinkingDeltaPayload,
    ToolStartPayload,
    ToolGeneratingPayload,
    ToolProgressPayload,
    ToolDeltaPayload,
    ToolCompletePayload,
    ErrorPayload,
    SessionInterruptedPayload,
    SessionRecalledPayload,
]


# Type-to-payload class mapping — canonical order per spec §6.2.
PAYLOAD_BY_TYPE: dict[CanonicalEventType, type] = {
    CanonicalEventType.MESSAGE_START: MessageStartPayload,
    CanonicalEventType.MESSAGE_DELTA: MessageDeltaPayload,
    CanonicalEventType.MESSAGE_COMPLETE: MessageCompletePayload,
    CanonicalEventType.REASONING_DELTA: ReasoningDeltaPayload,
    CanonicalEventType.REASONING_AVAILABLE: ReasoningAvailablePayload,
    CanonicalEventType.THINKING_DELTA: ThinkingDeltaPayload,
    CanonicalEventType.TOOL_START: ToolStartPayload,
    CanonicalEventType.TOOL_GENERATING: ToolGeneratingPayload,
    CanonicalEventType.TOOL_PROGRESS: ToolProgressPayload,
    CanonicalEventType.TOOL_DELTA: ToolDeltaPayload,
    CanonicalEventType.TOOL_COMPLETE: ToolCompletePayload,
    CanonicalEventType.ERROR: ErrorPayload,
    CanonicalEventType.SESSION_INTERRUPTED: SessionInterruptedPayload,
    CanonicalEventType.SESSION_RECALLED: SessionRecalledPayload,
}


@dataclass(frozen=True)
class CanonicalEvent:
    """spec §6.2 line 410-420 — typed union event.

    ``payload`` is one of the 14 dataclasses above — NEVER ``dict[str, Any]``.
    Use ``build`` factory or construct directly; the type/payload consistency
    is enforced at construction via ``__post_init__``.
    """

    type: CanonicalEventType
    seq: int              # SeqAllocator-issued, monotonic per session
    session_id: str
    run_id: str           # non-empty, not 'unknown'
    turn_id: str | None
    timestamp: int        # ms epoch
    payload: TypedPayload

    def __post_init__(self) -> None:
        expected = PAYLOAD_BY_TYPE.get(self.type)
        if expected is None:
            raise ValueError(f"unknown CanonicalEventType: {self.type!r}")
        if not isinstance(self.payload, expected):
            raise TypeError(
                f"CanonicalEvent.type={self.type.value!r} requires "
                f"payload of type {expected.__name__}, got "
                f"{type(self.payload).__name__}"
            )
        # These invariants prevent obvious rot on the wire.
        if not self.session_id:
            raise ValueError("CanonicalEvent.session_id is required")
        if not self.run_id or self.run_id == "unknown":
            raise ValueError("CanonicalEvent.run_id must be non-empty and not 'unknown'")


# spec-mandated arm count. Any drift here is a Phase A freeze violation.
CANONICAL_EVENT_ARM_COUNT = 14


__all__ = [
    "CanonicalEventType",
    "CANONICAL_EVENT_ARM_COUNT",
    "OriginatingRef",
    "MessageStartPayload",
    "MessageDeltaPayload",
    "MessageCompletePayload",
    "ReasoningDeltaPayload",
    "ReasoningAvailablePayload",
    "ThinkingDeltaPayload",
    "ToolStartPayload",
    "ToolGeneratingPayload",
    "ToolProgressPayload",
    "ToolDeltaPayload",
    "ToolCompletePayload",
    "ErrorPayload",
    "SessionInterruptedPayload",
    "SessionRecalledPayload",
    "TypedPayload",
    "PAYLOAD_BY_TYPE",
    "CanonicalEvent",
]
