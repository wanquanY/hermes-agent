"""Durable interaction lifecycle registry helpers.

The process-local ``PendingRegistry`` owns blocking/unblocking semantics. This
module owns the durable lifecycle projection: interaction request state is
persisted as internal run_events and is not part of the frontend canonical
timeline stream.
"""

from __future__ import annotations

import logging
from typing import Any

from hermes_agent.domain.interaction import InternalRunEventType
from hermes_agent.domain.interaction import InteractionFrameType
from tui_gateway.services.run_events import list_runtime_events

_log = logging.getLogger(__name__)

PUBLIC_TO_INTERNAL_EVENT_TYPE = {
    InteractionFrameType.REQUESTED.value: InternalRunEventType.INTERACTION_REQUESTED.value,
    InteractionFrameType.RESOLVED.value: InternalRunEventType.INTERACTION_RESOLVED.value,
    InteractionFrameType.EXPIRED.value: InternalRunEventType.INTERACTION_EXPIRED.value,
}


class InteractionRegistry:
    """Durable interaction lifecycle registry.

    The in-memory PendingRegistry owns blocking semantics. This service owns
    the persistence channel: every lifecycle transition is written as an
    internal run_event, and crash recovery is derived from those internal
    events. ``anchor_seq`` is caller-owned and never synthesized from the
    interaction event's own seq.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def persist(self, event_type: str, entry: Any) -> dict[str, Any]:
        """Persist one PendingRegistry lifecycle transition as an internal event."""

        db = self._db
        internal_type = PUBLIC_TO_INTERNAL_EVENT_TYPE.get(str(event_type or "").strip())
        if not internal_type:
            return {}
        if db is None or not hasattr(db, "append_run_event"):
            raise RuntimeError("interaction persistence requires append_run_event support")

        session_id = str(
            getattr(entry, "session_key", "")
            or getattr(entry, "conversation_id", "")
            or ""
        ).strip()
        if not session_id:
            raise ValueError("interaction persistence requires a stable session id")

        request_id = str(getattr(entry, "request_id", "") or "").strip()
        if not request_id:
            raise ValueError("interaction persistence requires request_id")

        kind = str(getattr(entry, "kind", "") or "").strip()
        status = _status_for_internal_event(internal_type, getattr(entry, "state", ""))
        anchor_seq = max(0, int(getattr(entry, "anchor_seq", 0) or 0))
        if internal_type != InternalRunEventType.INTERACTION_REQUESTED.value:
            anchor_seq = self.find_anchor_seq(session_id, request_id)

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
            "session_id": str(getattr(entry, "conversation_id", "") or session_id),
            # Wire compatibility only: storage ownership is the local
            # ``session_id`` variable above.
            "conversation_session_id": session_id,
            "runtime_scope_key": str(getattr(entry, "scope_key", "") or session_id),
            "payload": payload,
        }
        saved = db.append_run_event(session_id, frame)
        if not isinstance(saved, dict):
            raise RuntimeError(
                f"interaction persistence returned non-dict result type={internal_type} request_id={request_id}"
            )
        return saved

    def find_anchor_seq(self, session_id: str, request_id: str) -> int:
        """Return the caller-provided anchor seq for a persisted interaction."""

        db = self._db
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
                    SELECT anchor_seq
                      FROM run_events
                     WHERE session_id = ?
                       AND interaction_request_id = ?
                       AND event_type = ?
                     ORDER BY seq ASC
                     LIMIT 1
                    """,
                    (stable, rid, InternalRunEventType.INTERACTION_REQUESTED.value),
                ).fetchone()
        except Exception:
            _log.debug("interaction anchor lookup failed session=%s rid=%s", stable, rid, exc_info=True)
            return 0
        if row is None:
            return 0
        try:
            return int(row["anchor_seq"])
        except (KeyError, TypeError, ValueError, IndexError):
            return int(row[0] or 0)

    def list_pending(self, session_id: str) -> list[dict[str, Any]]:
        """Recover pending interactions from internal run_events for one session."""

        db = self._db
        if db is None:
            return []
        events = list_runtime_events(
            db,
            session_id,
            include_internal=True,
            limit=5000,
        )
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
            for value in sorted(
                by_request.values(),
                key=lambda item: (item["anchor_seq"] or item["seq"], item["request_id"]),
            )
            if value.get("status") == "pending"
        ]


def persist_interaction_event(db: Any, event_type: str, entry: Any) -> dict[str, Any]:
    return InteractionRegistry(db).persist(event_type, entry)


def find_interaction_anchor_seq(db: Any, session_id: str, request_id: str) -> int:
    return InteractionRegistry(db).find_anchor_seq(session_id, request_id)


def pending_interactions(db: Any, session_id: str) -> list[dict[str, Any]]:
    return InteractionRegistry(db).list_pending(session_id)


def _status_for_internal_event(internal_type: str, entry_state: Any) -> str:
    if internal_type.endswith(".requested"):
        return "pending"
    if internal_type.endswith(".resolved"):
        return "resolved"
    if internal_type.endswith(".expired"):
        return "expired"
    return str(entry_state or "").strip() or "pending"
