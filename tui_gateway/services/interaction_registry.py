"""Durable interaction lifecycle registry helpers.

The process-local ``PendingRegistry`` owns blocking/unblocking semantics. This
module owns the durable lifecycle projection: interaction request state is
persisted as internal run_events and is not part of the frontend canonical
timeline stream.
"""

from __future__ import annotations

from typing import Any

from hermes_agent.domain.interaction import InternalRunEventType
from hermes_agent.domain.interaction import InteractionFrameType
PUBLIC_TO_INTERNAL_EVENT_TYPE = {
    InteractionFrameType.REQUESTED.value: InternalRunEventType.INTERACTION_REQUESTED.value,
    InteractionFrameType.RESOLVED.value: InternalRunEventType.INTERACTION_RESOLVED.value,
    InteractionFrameType.EXPIRED.value: InternalRunEventType.INTERACTION_EXPIRED.value,
}

_INTERACTION_KINDS = frozenset({"approval", "clarify", "secret", "sudo"})
_REQUEST_TEXT_FIELDS = (
    "prompt",
    "question",
    "description",
    "env_var",
    "expires_at",
    "run_id",
    "turn_id",
    "participant_id",
    "activity_id",
    "activity_kind",
    "runtime_scope_key",
)


def _safe_request_payload(kind: str, value: Any) -> dict[str, Any]:
    """Keep only fields required to render and address a recovered prompt.

    Response values, sudo passwords, secret values, arbitrary metadata, and
    executable command text never enter the durable interaction projection.
    """
    source = value if isinstance(value, dict) else {}
    result = {
        field: str(source.get(field) or "").strip()
        for field in _REQUEST_TEXT_FIELDS
        if str(source.get(field) or "").strip()
    }
    choices = source.get("choices")
    if kind == "clarify" and isinstance(choices, list):
        normalized = [str(item or "").strip() for item in choices]
        result["choices"] = list(dict.fromkeys(item for item in normalized if item))
    raw_context = source.get("run_context")
    if isinstance(raw_context, dict):
        context = {
            key: str(raw_context.get(key) or "").strip()
            for key in (
                "participant_id",
                "activity_id",
                "activity_kind",
                "execution_scope_key",
                "runtime_scope_key",
            )
            if str(raw_context.get(key) or "").strip()
        }
        if context:
            result["run_context"] = context
    return result


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
        if db is None:
            raise RuntimeError("interaction persistence requires run component")

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
        if status == "pending":
            request = _safe_request_payload(
                kind,
                getattr(entry, "request_payload", None),
            )
            if request:
                payload["request"] = request
        if status == "resolved" and kind == "approval":
            choice = str(getattr(entry, "choice", "") or "").strip().lower()
            payload["decision"] = "deny" if choice == "deny" else "approved"

        frame = {
            "type": internal_type,
            "session_id": str(getattr(entry, "conversation_id", "") or session_id),
            # Wire compatibility only: storage ownership is the local
            # ``session_id`` variable above.
            "conversation_session_id": session_id,
            "runtime_scope_key": str(getattr(entry, "scope_key", "") or session_id),
            "payload": payload,
        }
        saved = db.runs.append_event(session_id, frame)
        if not isinstance(saved, dict):
            raise RuntimeError(
                f"interaction persistence returned non-dict result type={internal_type} request_id={request_id}"
            )
        return saved

    def find_anchor_seq(self, session_id: str, request_id: str) -> int:
        """Return the caller-provided anchor seq for a persisted interaction."""

        db = self._db
        if db is None:
            return 0
        stable = str(session_id or "").strip()
        rid = str(request_id or "").strip()
        if not stable or not rid:
            return 0
        return db.runs.interaction_anchor_seq(stable, rid)

    def list_pending(self, session_id: str) -> list[dict[str, Any]]:
        """Recover pending interactions from internal run_events for one session."""

        db = self._db
        if db is None:
            return []
        events = db.runs.list_events(
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
                **(
                    {"request": dict(payload.get("request"))}
                    if isinstance(payload.get("request"), dict)
                    else {}
                ),
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


def pending_interaction_replay_frames(
    db: Any,
    session_id: str,
    *,
    runtime_scope_key: str = "",
    run_id: str = "",
    active_run_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Project durable pending records into transient replay snapshots.

    The internal ledger remains the lifecycle source of truth. Subscribers get
    a render-ready, non-cursor-bearing frame on every reconnect so no second
    frontend pending store or guessed identity is required.
    """
    stable = str(session_id or "").strip()
    scope = str(runtime_scope_key or "").strip()
    selected_run = str(run_id or "").strip()
    frames: list[dict[str, Any]] = []
    for item in pending_interactions(db, stable):
        kind = str(item.get("kind") or "").strip()
        request_id = str(item.get("request_id") or "").strip()
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        owner_run_id = str(request.get("run_id") or "").strip()
        owner_scope = str(request.get("runtime_scope_key") or scope or stable).strip()
        if kind not in _INTERACTION_KINDS or not request_id or not owner_run_id:
            continue
        if scope and owner_scope != scope:
            continue
        if selected_run and owner_run_id != selected_run:
            continue
        if active_run_ids is not None and owner_run_id not in active_run_ids:
            continue
        source_seq = max(1, int(item.get("seq") or item.get("anchor_seq") or 1))
        payload = {
            **request,
            "request_id": request_id,
            "kind": kind,
            "status": "pending",
            "source_event_type": f"{kind}.request",
            "replay_snapshot": True,
        }
        frames.append({
            "type": "interaction.requested",
            "conversation_session_id": stable,
            "session_id": stable,
            "run_id": owner_run_id,
            "turn_id": str(request.get("turn_id") or "").strip(),
            "participant_id": str(request.get("participant_id") or "agent").strip(),
            "runtime_scope_key": owner_scope,
            "transient": True,
            "source_seq": source_seq,
            "runtime_source_seq": source_seq,
            "payload": payload,
        })
    return frames


def _status_for_internal_event(internal_type: str, entry_state: Any) -> str:
    if internal_type.endswith(".requested"):
        return "pending"
    if internal_type.endswith(".resolved"):
        return "resolved"
    if internal_type.endswith(".expired"):
        return "expired"
    return str(entry_state or "").strip() or "pending"
