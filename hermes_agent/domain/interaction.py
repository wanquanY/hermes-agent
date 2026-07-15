"""spec §7.4 Interaction 双通道类型契约.

Interaction requires two orthogonal channels (spec §7.4.1):

* **Persistence channel** — internal ``run_events`` writes with
  ``InternalRunEventType._internal.interaction.*`` prefix. Never leaks to
  the wire; drives crash-recovery on restart.
* **Delivery channel** — independent ``InteractionFrame`` wire frame
  routed to the frontend ``interaction-store`` SSoT (spec §7.4.3 line
  796). Does NOT ride the canonical event stream.

The two channels share ``request_id`` for correlation and ``anchor_seq``
for stable frontend ordering.

Audit 2026-07-07 (docs/v3_audit_report.md §四 伪绿 3) flagged:
* No ``InteractionFrame`` dataclass (was raw dict).
* No ``InteractionFrameType`` / ``InternalRunEventType`` enum
  (was string concatenation ``f"interaction.{status}"``).
* No ``InteractionRegistry`` class (was scattered helper functions).
* ``anchor_seq`` semantics wrong — was set to the interaction's own seq
  instead of the triggering event's seq. spec §7.4.4 line 808:
  ``anchor_seq`` is a **required** keyword-only argument the caller must
  pass — usually the seq of the ``tool.complete`` that triggered the
  approval.

This module defines the frozen types + Protocol so callers depend on a
typed contract, not string fragments.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# §7.4.2 Persistence channel — internal run_events subtype
# ---------------------------------------------------------------------------


class InternalRunEventType(str, Enum):
    """Backend-internal event types persisted to ``run_events`` for crash
    recovery. Never surfaced to the wire. The ``_internal.`` prefix keeps
    them out of ``CanonicalEventType`` (spec §6.2 line 405-407 forbids
    ``interaction.*`` there).
    """

    INTERACTION_REQUESTED = "_internal.interaction.requested"
    INTERACTION_RESOLVED = "_internal.interaction.resolved"
    INTERACTION_EXPIRED = "_internal.interaction.expired"


# ---------------------------------------------------------------------------
# §7.4.3 Delivery channel — wire frame
# ---------------------------------------------------------------------------


class InteractionFrameType(str, Enum):
    """Wire frame types delivered to the frontend ``interaction-store`` SSoT
    (spec §7.4.3). Distinct from CanonicalEventType — routed as a
    **top-level frame**, not embedded in the event stream.
    """

    REQUESTED = "interaction.requested"
    RESOLVED = "interaction.resolved"
    EXPIRED = "interaction.expired"


InteractionKind = Literal["approval", "clarify", "gateway-confirm"]


@dataclass(frozen=True)
class InteractionFrame:
    """spec §7.4.3 line 784-794 — the wire frame delivered independently
    of the canonical event stream.

    ``anchor_seq`` (spec §7.4 P1-B5, line 791) shares the ``items[].seq``
    domain with canonical events so the frontend R4 rule can position the
    interaction chip stably. It is the seq of the **triggering** event
    (e.g. ``tool.complete``), NOT the interaction's own seq — that was
    the bug fixed by this contract (docs/v3_audit_report.md §四 伪绿 3).
    """

    type: InteractionFrameType
    request_id: str
    kind: InteractionKind
    run_id: str | None
    turn_id: str | None
    anchor_seq: int
    payload: dict[str, Any]
    timestamp: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("InteractionFrame.request_id is required")
        if self.anchor_seq < 0:
            raise ValueError(
                f"InteractionFrame.anchor_seq must be >= 0, got {self.anchor_seq}"
            )


# ---------------------------------------------------------------------------
# §7.4.4 Registry API — Protocol only (concrete impl lands with Phase D switch)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InteractionRequest:
    """In-memory handle returned by ``request()`` — includes the
    correlated ``request_id`` + ``anchor_seq`` for downstream use.
    """

    request_id: str
    run_id: str | None
    kind: InteractionKind
    anchor_seq: int
    payload: dict[str, Any]
    timestamp: int


@dataclass(frozen=True)
class InteractionResponse:
    """The caller's answer to a pending interaction — supplied to
    ``resolve``.
    """

    request_id: str
    response_payload: dict[str, Any]


@runtime_checkable
class InteractionRegistry(Protocol):
    """spec §7.4.4 registry facade.

    Implementations run inside the aggregate boundary — persisting to
    run_events (internal channel) AND dispatching to the wire (delivery
    channel). ``anchor_seq`` is a **required** keyword-only argument on
    ``request`` so callers cannot omit it and silently trigger the
    "interaction's own seq" bug (audit §四 伪绿 3).
    """

    def request(
        self,
        run_id: str,
        kind: InteractionKind,
        payload: dict[str, Any],
        *,
        anchor_seq: int,
    ) -> InteractionRequest:
        """1. Append ``InternalRunEventType.INTERACTION_REQUESTED`` to
        ``run_events`` (persistence channel).
        2. Emit ``InteractionFrame(type=REQUESTED, anchor_seq=anchor_seq)``
        on the wire (delivery channel).
        3. Return the in-memory ``InteractionRequest`` handle.

        ``anchor_seq`` MUST be the seq of the event that triggered this
        interaction — typically a ``tool.complete``. Not the interaction's
        own seq.
        """
        ...

    def resolve(self, request_id: str, response: InteractionResponse) -> None:
        """Append INTERACTION_RESOLVED + emit RESOLVED frame."""
        ...

    def expire(self, request_id: str) -> None:
        """Append INTERACTION_EXPIRED + emit EXPIRED frame."""
        ...

    def list_pending(self, session_id: str) -> list[InteractionRequest]:
        """REQUESTED − (RESOLVED ∪ EXPIRED) as a set-difference read model."""
        ...


__all__ = [
    "InternalRunEventType",
    "InteractionFrameType",
    "InteractionFrame",
    "InteractionKind",
    "InteractionRequest",
    "InteractionResponse",
    "InteractionRegistry",
]
