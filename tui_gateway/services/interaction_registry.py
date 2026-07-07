"""Durable interaction lifecycle registry helpers.

The process-local ``PendingRegistry`` owns blocking/unblocking semantics. This
module owns the durable lifecycle projection: interaction request state is
persisted as internal run_events and is not part of the frontend canonical
timeline stream.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

PUBLIC_TO_INTERNAL_EVENT_TYPE = {
    "interaction.requested": "_internal.interaction.requested",
    "interaction.resolved": "_internal.interaction.resolved",
    "interaction.expired": "_internal.interaction.expired",
}


def persist_interaction_event(db: Any, event_type: str, entry: Any) -> dict[str, Any]:
    """Persist one PendingRegistry lifecycle transition as an internal event."""

    if db is None or not hasattr(db, "append_run_event"):
        return {}
    internal_type = PUBLIC_TO_INTERNAL_EVENT_TYPE.get(str(event_type or "").strip())
    if not internal_type:
        return {}

    stored_session_id = str(
        getattr(entry, "session_key", "")
        or getattr(entry, "conversation_id", "")
        or ""
    ).strip()
    if not stored_session_id:
        return {}

    request_id = str(getattr(entry, "request_id", "") or "").strip()
    if not request_id:
        return {}

    kind = str(getattr(entry, "kind", "") or "").strip()
    status = _status_for_internal_event(internal_type, getattr(entry, "state", ""))
    anchor_seq = 0
    if internal_type != "_internal.interaction.requested":
        anchor_seq = find_interaction_anchor_seq(db, stored_session_id, request_id)

    payload: dict[str, Any] = {
        "interaction_request_id": request_id,
        "request_id": request_id,
        "interaction_kind": kind,
        "kind": kind,
        "interaction_status": status,
        "status": status,
        "state": status,
        "anchor_seq": anchor_seq,
    }
    if status == "resolved":
        payload["choice"] = getattr(entry, "choice", None)

    frame = {
        "type": internal_type,
        "session_id": str(getattr(entry, "conversation_id", "") or stored_session_id),
        "stored_session_id": stored_session_id,
        "runtime_scope_key": str(getattr(entry, "scope_key", "") or stored_session_id),
        "payload": payload,
    }
    saved = db.append_run_event(stored_session_id, frame)
    if (
        internal_type == "_internal.interaction.requested"
        and isinstance(saved, dict)
        and int(saved.get("seq") or 0) > 0
    ):
        _set_requested_anchor_seq(db, stored_session_id, request_id, int(saved["seq"]))
        saved = dict(saved)
        saved["anchor_seq"] = int(saved["seq"])
        saved_payload = saved.get("payload")
        if isinstance(saved_payload, dict):
            saved_payload["anchor_seq"] = int(saved["seq"])
    return saved if isinstance(saved, dict) else {}


def find_interaction_anchor_seq(db: Any, session_id: str, request_id: str) -> int:
    """Return the request event seq for a persisted interaction, if known."""

    conn = getattr(db, "_conn", None)
    lock = getattr(db, "_lock", None)
    if conn is None or lock is None:
        return 0
    stable = str(session_id or "").strip()
    rid = str(request_id or "").strip()
    if not stable or not rid:
        return 0
    try:
        with lock:
            row = conn.execute(
                """
                SELECT seq
                  FROM run_events
                 WHERE session_id = ?
                   AND interaction_request_id = ?
                   AND event_type = '_internal.interaction.requested'
                 ORDER BY seq ASC
                 LIMIT 1
                """,
                (stable, rid),
            ).fetchone()
    except Exception:
        _log.debug("interaction anchor lookup failed session=%s rid=%s", stable, rid, exc_info=True)
        return 0
    if row is None:
        return 0
    try:
        return int(row["seq"])
    except Exception:
        return int(row[0] or 0)


def pending_interactions(db: Any, session_id: str) -> list[dict[str, Any]]:
    """Recover pending interactions from internal run_events for one session."""

    if db is None or not hasattr(db, "list_run_events"):
        return []
    events = db.list_run_events(session_id, include_internal=True, limit=5000)
    by_request: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if not event_type.startswith("_internal.interaction."):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        request_id = str(
            payload.get("interaction_request_id")
            or payload.get("request_id")
            or ""
        ).strip()
        if not request_id:
            continue
        status = str(
            payload.get("interaction_status")
            or payload.get("status")
            or payload.get("state")
            or ""
        ).strip()
        by_request[request_id] = {
            "request_id": request_id,
            "kind": str(payload.get("interaction_kind") or payload.get("kind") or ""),
            "status": status,
            "anchor_seq": int(payload.get("anchor_seq") or 0),
            "seq": int(event.get("seq") or 0),
        }
    return [
        value
        for value in sorted(by_request.values(), key=lambda item: (item["anchor_seq"] or item["seq"], item["request_id"]))
        if value.get("status") == "pending"
    ]


def _status_for_internal_event(internal_type: str, entry_state: Any) -> str:
    if internal_type.endswith(".requested"):
        return "pending"
    if internal_type.endswith(".resolved"):
        return "resolved"
    if internal_type.endswith(".expired"):
        return "expired"
    return str(entry_state or "").strip() or "pending"


def _set_requested_anchor_seq(db: Any, session_id: str, request_id: str, seq: int) -> None:
    conn = getattr(db, "_conn", None)
    lock = getattr(db, "_lock", None)
    if conn is None or lock is None:
        return
    try:
        with lock:
            conn.execute(
                """
                UPDATE run_events
                   SET anchor_seq = ?
                 WHERE session_id = ?
                   AND interaction_request_id = ?
                   AND event_type = '_internal.interaction.requested'
                   AND seq = ?
                """,
                (int(seq), str(session_id), str(request_id), int(seq)),
            )
    except Exception:
        _log.debug("interaction anchor update failed session=%s rid=%s", session_id, request_id, exc_info=True)
