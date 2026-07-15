"""Run lifecycle transition domain service.

The runs table is a materialized lifecycle view over canonical run_events.
This module owns the status vocabulary and transition policy; storage code is
responsible for persistence, duplicate-active checks, and projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from hermes_team_mission.runtime.run_event_retention import COALESCIBLE_STREAM_EVENT_TYPES


ACTIVE_RUN_STATUSES = frozenset({
    "queued",
    "starting",
    "running",
    "waiting_approval",
    "cancelling",
    "finalizing",
})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "interrupted", "cancelled"})
TERMINAL_RUN_STATUS_RANK = {
    "cancelled": 1,
    "interrupted": 1,
    "failed": 1,
    "completed": 2,
}
RUN_OPENING_EVENT_TYPES = frozenset({
    "message.start",
    "message.delta",
    "reasoning.delta",
    "thinking.delta",
    "tool.start",
    "tool.generating",
    "tool.progress",
    "approval.request",
    "secret.request",
    "sudo.request",
    "input_approval.request",
    *COALESCIBLE_STREAM_EVENT_TYPES,
})


@dataclass(frozen=True)
class RunStatusTransition:
    should_track: bool
    status: str
    opens_active: bool
    terminal_status: str | None = None


def terminal_status_from_event(event_type: str, payload: Mapping[str, Any]) -> str | None:
    normalized_type = str(event_type or "").strip()
    if normalized_type == "error":
        return "failed"
    if normalized_type == "session.recalled":
        return "interrupted"
    if normalized_type != "message.complete":
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status == "interrupted":
        return "interrupted"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"error", "failed"}:
        return "failed"
    return "completed"


def prefer_terminal_run_status(existing_status: str, incoming_status: str) -> str:
    existing = str(existing_status or "").strip().lower()
    incoming = str(incoming_status or "").strip().lower()
    if existing not in TERMINAL_RUN_STATUSES or incoming not in TERMINAL_RUN_STATUSES:
        return incoming or existing
    existing_rank = TERMINAL_RUN_STATUS_RANK.get(existing, 0)
    incoming_rank = TERMINAL_RUN_STATUS_RANK.get(incoming, 0)
    if incoming_rank > existing_rank:
        return incoming
    return existing


def event_opens_active_run(event_type: str) -> bool:
    return str(event_type or "").strip() in RUN_OPENING_EVENT_TYPES


def resolve_run_status_transition(
    *,
    event_type: str,
    existing_status: str = "",
    terminal_status: str | None = None,
    has_existing_run: bool = False,
) -> RunStatusTransition:
    normalized_existing = str(existing_status or "").strip().lower()
    normalized_terminal = str(terminal_status or "").strip().lower() or None
    opens_active = event_opens_active_run(event_type)
    should_track = bool(has_existing_run or normalized_terminal or opens_active)
    if not should_track:
        return RunStatusTransition(
            should_track=False,
            status="",
            opens_active=opens_active,
            terminal_status=normalized_terminal,
        )
    if normalized_existing in TERMINAL_RUN_STATUSES and normalized_terminal is None:
        status = normalized_existing
    elif normalized_existing in TERMINAL_RUN_STATUSES and normalized_terminal in TERMINAL_RUN_STATUSES:
        status = prefer_terminal_run_status(normalized_existing, normalized_terminal)
    elif opens_active:
        status = "running"
    else:
        status = normalized_terminal or normalized_existing or "running"
    return RunStatusTransition(
        should_track=True,
        status=status,
        opens_active=opens_active,
        terminal_status=normalized_terminal,
    )


def resolve_explicit_run_status(
    *,
    existing_status: str,
    incoming_status: str,
) -> str:
    existing = str(existing_status or "").strip().lower()
    incoming = str(incoming_status or "").strip().lower() or "running"
    if existing in TERMINAL_RUN_STATUSES and incoming not in TERMINAL_RUN_STATUSES:
        return existing
    if existing in TERMINAL_RUN_STATUSES and incoming in TERMINAL_RUN_STATUSES:
        return prefer_terminal_run_status(existing, incoming)
    return incoming


def error_for_status(
    *,
    status: str,
    payload: Mapping[str, Any],
    existing_error: str = "",
    duplicate_active_error: str = "",
) -> str:
    normalized = str(status or "").strip().lower()
    if duplicate_active_error:
        return duplicate_active_error
    if normalized == "failed":
        return str(payload.get("message") or "") or str(existing_error or "")
    if normalized in TERMINAL_RUN_STATUSES:
        return ""
    return str(payload.get("message") or "") or str(existing_error or "")
