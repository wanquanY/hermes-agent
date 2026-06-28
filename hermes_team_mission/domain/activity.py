"""ADR-0001 Activity domain model.

Lives beside run_context.py as the in-memory representation for Activity
runtime primitives and durable Activity Command intent rows.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


# Allowed activity kinds: mirror the activities table CHECK constraint.
ACTIVITY_KINDS = frozenset({
    "chat",
    "agent_dispatch",
    "team_dispatch",
    "member_chat",
    "mission",
})

# Activity Command kinds: mirror the activity_commands table CHECK constraint.
ACTIVITY_COMMAND_KINDS = frozenset({"create", "start", "cancel", "complete"})

# Activity Command state machine.
ACTIVITY_COMMAND_STATES = frozenset({
    "accepted",
    "dispatched",
    "satisfied",
    "failed",
})

# Legal state transitions. The internal reconciler introduced in 1.C advances
# commands through this graph.
_ALLOWED_TRANSITIONS = {
    "accepted": frozenset({"dispatched", "satisfied", "failed"}),
    "dispatched": frozenset({"satisfied", "failed"}),
    "satisfied": frozenset(),
    "failed": frozenset(),
}


def is_legal_transition(prev: str, next_state: str) -> bool:
    return next_state in _ALLOWED_TRANSITIONS.get(prev, frozenset())


@dataclass(frozen=True)
class Activity:
    """ADR-0001: runtime-elevated Activity domain object.

    Maps to the existing `activities` SQL table; this dataclass is the
    in-memory representation used by Command bus / reconciler.
    """

    activity_id: str
    kind: str
    conversation_id: str
    parent_activity_id: Optional[str] = None
    status: str = "pending"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.activity_id or "").strip():
            raise ValueError("activity_id is required")
        if self.kind not in ACTIVITY_KINDS:
            raise ValueError(f"kind must be one of {sorted(ACTIVITY_KINDS)}")
        if not str(self.conversation_id or "").strip():
            raise ValueError("conversation_id is required")


@dataclass(frozen=True)
class ActivityCommand:
    """ADR-0001: durable intent for an Activity state transition.

    Persisted in activity_commands. The reconciler (1.C) consumes
    accepted/dispatched rows and advances state by emitting durable run_events.
    """

    command_id: str
    activity_id: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    intent_at: float = 0.0
    state: str = "accepted"
    state_changed_at: float = 0.0
    result_event_id: Optional[int] = None
    error_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.command_id or "").strip():
            raise ValueError("command_id is required")
        if not str(self.activity_id or "").strip():
            raise ValueError("activity_id is required")
        if self.kind not in ACTIVITY_COMMAND_KINDS:
            raise ValueError(f"kind must be one of {sorted(ACTIVITY_COMMAND_KINDS)}")
        if self.state not in ACTIVITY_COMMAND_STATES:
            raise ValueError(f"state must be one of {sorted(ACTIVITY_COMMAND_STATES)}")

    def with_state(
        self,
        next_state: str,
        *,
        error_reason: str = "",
        result_event_id: Optional[int] = None,
    ) -> "ActivityCommand":
        """Return a new instance with the command state transitioned."""
        if not is_legal_transition(self.state, next_state):
            raise ValueError(
                f"illegal command state transition: {self.state} -> {next_state}"
            )
        return dataclasses.replace(
            self,
            state=next_state,
            state_changed_at=time.time(),
            error_reason=error_reason or self.error_reason,
            result_event_id=(
                result_event_id
                if result_event_id is not None
                else self.result_event_id
            ),
        )


# Activity Command bus event type constants - Phase 1.C reconciler emits
# these via run_control.record_event so subscribers / replay get a
# canonical timeline of every command's state change.
#
# Namespace MUST stay under `activity.command.*` to avoid colliding with
# the existing activities-table lifecycle events emitted by
# tui_gateway/services/worker_frame_router.py (activity.running /
# activity.completed).
ACTIVITY_COMMAND_EVENT_TYPES = frozenset({
    "activity.command.created",        # create command satisfied
    "activity.command.start.accepted", # start command dispatched
    "activity.command.run.spawned",    # worker spawn acked (1.D will emit)
    "activity.command.run.started",    # worker run.started observed
    "activity.command.run.failed",     # spawn / run failed durably
    "activity.command.cancelled",      # cancel command satisfied
    "activity.command.completed",      # complete command satisfied
})
