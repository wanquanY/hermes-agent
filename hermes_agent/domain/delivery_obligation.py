"""Domain contracts for durable final-response delivery obligations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class DeliveryObligationState(str, Enum):
    PENDING = "pending"
    ATTEMPTING = "attempting"
    DELIVERED = "delivered"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class DeliveryObligation:
    obligation_id: str
    session_key: str
    platform: str
    chat_id: str
    thread_id: Optional[str]
    reply_to: Optional[str]
    metadata: Mapping[str, Any]
    content: str
    state: DeliveryObligationState
    attempts: int
    created_at: float
    updated_at: float
    owner_pid: Optional[int]
    owner_started_at: Optional[int]
    last_error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RecoverableDelivery:
    obligation: DeliveryObligation
    needs_duplicate_marker: bool


RECOVERED_DELIVERY_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\n\n"
)


def compute_delivery_obligation_id(
    session_key: str,
    inbound_message_id: str,
    content: str,
) -> str:
    """Return a stable turn-and-content identifier for idempotent recording."""
    payload = f"{session_key}|{inbound_message_id}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


__all__ = [
    "DeliveryObligation",
    "DeliveryObligationState",
    "RECOVERED_DELIVERY_MARKER",
    "RecoverableDelivery",
    "compute_delivery_obligation_id",
]
