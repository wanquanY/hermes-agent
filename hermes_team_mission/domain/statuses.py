from __future__ import annotations

from typing import Any


MISSION_STATUS_VARIANTS = {
    "active": "running",
    "ready": "running",
    "blocked_waiting_dependency": "waiting_dependency",
    "complete": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "cancelling": "cancelled",
    "canceling": "cancelled",
    "interrupted": "interrupted",
}

MISSION_STATUSES = frozenset({
    "draft",
    "planning",
    "waiting_approval",
    "waiting_dependency",
    "running",
    "partially_blocked",
    "blocked",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
})

TERMINAL_MISSION_STATUSES = frozenset({
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "interrupted",
})

TERMINAL_MISSION_STATUS_ALIASES = frozenset({
    raw
    for raw in (*TERMINAL_MISSION_STATUSES, *MISSION_STATUS_VARIANTS)
    if MISSION_STATUS_VARIANTS.get(raw, raw) in TERMINAL_MISSION_STATUSES
})


def normalize_status_key(value: Any) -> str:
    return "_".join(str(value or "").strip().lower().replace("-", " ").split())


def normalize_mission_status(value: Any) -> str:
    raw = normalize_status_key(value)
    if not raw:
        return ""
    status = MISSION_STATUS_VARIANTS.get(raw, raw)
    return status if status in MISSION_STATUSES else ""


def is_terminal_mission_status(value: Any) -> bool:
    status = normalize_mission_status(value)
    return bool(status) and status in TERMINAL_MISSION_STATUSES


def is_cancelled_mission_status(value: Any) -> bool:
    return normalize_mission_status(value) == "cancelled"


def terminal_mission_sql_literals(*, include: tuple[str, ...] = ()) -> str:
    statuses = sorted({*TERMINAL_MISSION_STATUS_ALIASES, *include})
    return ",".join(
        f"'{escaped}'"
        for status in statuses
        for escaped in (status.replace("'", "''"),)
    )


def terminal_run_status_for_mission(value: Any) -> str:
    status = normalize_mission_status(value)
    if status == "completed":
        return "completed"
    if status == "failed" or status == "blocked":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status == "interrupted":
        return "interrupted"
    return ""


def projected_state_for_mission_status(value: Any) -> str:
    status = normalize_mission_status(value)
    if status in TERMINAL_MISSION_STATUSES:
        return status
    return ""
