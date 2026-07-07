"""Hermes domain services (spec §2).

Exposes the canonical event contract (§6.2) so callers can import from
``hermes_agent.domain`` without deep paths.
"""

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
from hermes_agent.domain.interaction import (
    InteractionFrame,
    InteractionFrameType,
    InteractionKind,
    InteractionRegistry,
    InteractionRequest,
    InteractionResponse,
    InternalRunEventType,
)

__all__ = [
    "CANONICAL_EVENT_ARM_COUNT",
    "CanonicalEvent",
    "CanonicalEventType",
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
    "InternalRunEventType",
    "InteractionFrameType",
    "InteractionFrame",
    "InteractionKind",
    "InteractionRequest",
    "InteractionResponse",
    "InteractionRegistry",
]
