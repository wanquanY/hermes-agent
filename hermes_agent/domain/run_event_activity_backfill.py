"""Stable activity identity inference for legacy run events."""

from __future__ import annotations

import json
import re
from typing import Any

ACTIVITY_ID_BACKFILL_PROGRESS_KEY = "activity_id_backfill_v1_max_id"
ACTIVITY_ID_BACKFILL_DONE_KEY = "activity_id_backfill_v1_done"

_NODE_SESSION_PATTERN = re.compile(r"^team:(mission-[A-Za-z0-9]+):node:")
_LEADER_SESSION_PATTERN = re.compile(
    r"^team-session-team-conversation-([A-Za-z0-9-]+)$"
)


def activity_id_from_session_id(session_id: str) -> str:
    stable = str(session_id or "").strip()
    if not stable:
        return ""
    node_match = _NODE_SESSION_PATTERN.match(stable)
    if node_match:
        return f"mission:{node_match.group(1)}"
    leader_match = _LEADER_SESSION_PATTERN.match(stable)
    if leader_match:
        return f"team-conversation:{leader_match.group(1)}"
    return f"chat:{stable}"


def activity_id_from_event_json(event_json: Any) -> str:
    if not event_json:
        return ""
    try:
        frame = (
            json.loads(event_json)
            if isinstance(event_json, (str, bytes))
            else event_json
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(frame, dict):
        return ""
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    metadata = (
        frame.get("metadata") if isinstance(frame.get("metadata"), dict) else {}
    )
    return str(
        frame.get("activity_id")
        or frame.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
        or metadata.get("activity_id")
        or metadata.get("activityId")
        or ""
    ).strip()


def activity_id_for_legacy_event(row: Any) -> str:
    event_json = row["event_json"] if "event_json" in row.keys() else None
    from_frame = activity_id_from_event_json(event_json)
    if from_frame:
        return from_frame
    session_id = row["session_id"] if "session_id" in row.keys() else ""
    return activity_id_from_session_id(session_id)


__all__ = [
    "ACTIVITY_ID_BACKFILL_DONE_KEY",
    "ACTIVITY_ID_BACKFILL_PROGRESS_KEY",
    "activity_id_for_legacy_event",
    "activity_id_from_event_json",
    "activity_id_from_session_id",
]
