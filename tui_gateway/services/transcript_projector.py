"""Project runtime message events into visible conversation transcript rows.

Phase 1 keeps this service independent from ``SessionDB`` so the projection
contract is reviewable before it is wired into live persistence. The storage
adapter only needs a key-value upsert by the conversation message identity.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


_MESSAGE_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "message.complete",
    "reasoning.delta",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _event_text(event: dict[str, Any], *, terminal: bool = False) -> str:
    payload = _payload(event)
    keys = (
        ("text", "content", "output", "final_response", "finalResponse", "summary")
        if terminal
        else ("delta", "text", "content", "output")
    )
    return _first_text(*(payload.get(key) for key in keys))


def _event_status(event: dict[str, Any]) -> str:
    payload = _payload(event)
    status = _text(payload.get("status")).lower()
    if status in {"complete", "completed"}:
        return "completed"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"interrupted", "failed", "error"}:
        return "failed" if status == "error" else status
    if _text(event.get("type")) == "message.complete":
        return "completed"
    return "streaming"


def _event_reasoning_text(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return _first_text(
        payload.get("delta"),
        payload.get("text"),
        payload.get("reasoning"),
        payload.get("reasoning_content"),
        payload.get("reasoningContent"),
    )


@dataclass(frozen=True)
class ProjectionDiagnostic:
    code: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectionKey:
    session_id: str
    run_id: str
    message_seq_in_run: str


@dataclass(frozen=True)
class ProjectedTranscriptMessage:
    conversation_message_id: str
    session_id: str
    role: str
    content: str
    participant_id: str
    metadata: dict[str, Any]
    status: str = "streaming"
    reasoning: str = ""

    @property
    def key(self) -> ProjectionKey:
        return ProjectionKey(
            session_id=self.session_id,
            run_id=_text(self.metadata.get("run_id")),
            message_seq_in_run=_text(self.metadata.get("message_seq_in_run")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_message_id": self.conversation_message_id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "participant_id": self.participant_id,
            "metadata": copy.deepcopy(self.metadata),
            "status": self.status,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class ProjectionResult:
    applied: bool
    action: str
    message: ProjectedTranscriptMessage | None = None
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()


class TranscriptProjectionStore(Protocol):
    def get_projected_message(self, key: ProjectionKey) -> ProjectedTranscriptMessage | None:
        ...

    def upsert_projected_message(self, message: ProjectedTranscriptMessage) -> ProjectedTranscriptMessage:
        ...


class InMemoryTranscriptProjectionStore:
    """Small adapter used by contract tests and future adapter smoke tests."""

    def __init__(self) -> None:
        self._messages: dict[ProjectionKey, ProjectedTranscriptMessage] = {}

    def get_projected_message(self, key: ProjectionKey) -> ProjectedTranscriptMessage | None:
        message = self._messages.get(key)
        return replace(message, metadata=copy.deepcopy(message.metadata)) if message else None

    def upsert_projected_message(self, message: ProjectedTranscriptMessage) -> ProjectedTranscriptMessage:
        stored = replace(message, metadata=copy.deepcopy(message.metadata))
        self._messages[message.key] = stored
        return replace(stored, metadata=copy.deepcopy(stored.metadata))

    def list_messages(self) -> list[ProjectedTranscriptMessage]:
        return [
            replace(message, metadata=copy.deepcopy(message.metadata))
            for message in self._messages.values()
        ]


class SessionDBTranscriptProjectionStore:
    """Projection store backed by ``SessionDB.messages``.

    The adapter keeps the dependency direction from gateway services to the
    state layer. ``hermes_state`` owns the SQL upsert, while this class owns the
    projector's domain mapping.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def get_projected_message(self, key: ProjectionKey) -> ProjectedTranscriptMessage | None:
        row = self._db.get_message_by_conversation_message_id(
            key.session_id,
            conversation_message_id_for(key),
        )
        return self._row_to_projected_message(row)

    def upsert_projected_message(self, message: ProjectedTranscriptMessage) -> ProjectedTranscriptMessage:
        row = self._db.upsert_projected_conversation_message(
            session_id=message.session_id,
            conversation_message_id=message.conversation_message_id,
            role=message.role,
            content=message.content,
            participant_id=message.participant_id,
            metadata=message.metadata,
            status=message.status,
            reasoning=message.reasoning,
        )
        projected = self._row_to_projected_message(row)
        if projected is None:
            raise RuntimeError("projected message upsert did not return a row")
        return projected

    @staticmethod
    def _row_to_projected_message(row: Any) -> ProjectedTranscriptMessage | None:
        if not isinstance(row, dict):
            return None
        metadata = row.get("metadata")
        metadata = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
        conversation_message_id = _text(
            row.get("conversation_message_id")
            or metadata.get("conversation_message_id")
        )
        session_id = _text(row.get("session_id") or metadata.get("session_id"))
        if not session_id:
            session_id = _text(metadata.get("stored_session_id") or metadata.get("storedSessionId"))
        if not conversation_message_id or not session_id:
            return None
        participant_id = _text(
            row.get("participant_id")
            or metadata.get("participant_id")
            or metadata.get("participantId")
        )
        return ProjectedTranscriptMessage(
            conversation_message_id=conversation_message_id,
            session_id=session_id,
            role=_text(row.get("role")) or "assistant",
            content=row.get("content") if isinstance(row.get("content"), str) else "",
            participant_id=participant_id,
            metadata=metadata,
            status=_text(metadata.get("projection_status")) or "streaming",
            reasoning=row.get("reasoning") if isinstance(row.get("reasoning"), str) else "",
        )


def conversation_message_id_for(key: ProjectionKey) -> str:
    raw = f"{key.session_id}\0{key.run_id}\0{key.message_seq_in_run}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"msg_{digest}"


def conversation_user_message_id_for(
    *,
    session_id: str,
    turn_id: str,
    run_id: str = "",
    client_message_id: str = "",
) -> str:
    """Stable id for a user-submission row in a visible conversation.

    Team conversation user turns are created at submit time, not from worker
    message events. ``turn_id`` is the primary identity because the same text
    can be sent repeatedly in different turns; ``run_id`` and
    ``client_message_id`` are fallback entropy for older callers.
    """
    stable_session_id = _text(session_id)
    stable_turn_id = _text(turn_id)
    stable_run_id = _text(run_id)
    stable_client_message_id = _text(client_message_id)
    identity = stable_turn_id or stable_client_message_id or stable_run_id
    if not stable_session_id or not identity:
        raise ValueError("session_id and one user message identity are required")
    raw = f"{stable_session_id}\0user\0{identity}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"msg_{digest}"


class TranscriptProjector:
    """Pure event-to-transcript reducer.

    The canonical assistant identity is ``(session_id, run_id,
    message_seq_in_run)``. A single run may emit multiple visible assistant
    messages, so run id alone is never a valid assistant-message key.
    """

    def __init__(self, store: TranscriptProjectionStore) -> None:
        self._store = store

    def reduce(self, event: dict[str, Any]) -> ProjectionResult:
        if not isinstance(event, dict):
            return ProjectionResult(
                applied=False,
                action="ignored",
                diagnostics=(
                    ProjectionDiagnostic("invalid-event", "event must be a dict"),
                ),
            )

        event_type = _text(event.get("type"))
        if event_type not in _MESSAGE_EVENT_TYPES:
            return ProjectionResult(applied=False, action="ignored")

        payload = _payload(event)
        session_id = _text(
            event.get("stored_session_id")
            or event.get("storedSessionId")
            or payload.get("stored_session_id")
            or payload.get("storedSessionId")
            or event.get("session_id")
            or event.get("sessionId")
        )
        run_id = _text(event.get("run_id") or event.get("runId") or payload.get("run_id") or payload.get("runId"))
        participant_id = _text(
            event.get("participant_id")
            or event.get("participantId")
            or payload.get("participant_id")
            or payload.get("participantId")
        )
        message_seq, legacy_identity = self._message_seq(event)
        diagnostics: list[ProjectionDiagnostic] = []
        if legacy_identity:
            diagnostics.append(
                ProjectionDiagnostic(
                    "legacy-message-identity",
                    "message_seq_in_run missing; using source event seq as legacy identity",
                    {"run_id": run_id, "message_seq_in_run": message_seq},
                )
            )

        missing = [
            name
            for name, value in (
                ("session_id", session_id),
                ("run_id", run_id),
                ("message_seq_in_run", message_seq),
            )
            if not value
        ]
        if missing:
            return ProjectionResult(
                applied=False,
                action="skipped",
                diagnostics=(
                    ProjectionDiagnostic(
                        "identity-incomplete",
                        "message event is missing required transcript identity",
                        {"missing": missing, "event_type": event_type},
                    ),
                    *diagnostics,
                ),
            )
        if not participant_id:
            return ProjectionResult(
                applied=False,
                action="skipped",
                diagnostics=(
                    ProjectionDiagnostic(
                        "participant-unresolved",
                        "message event is missing participant_id; refusing leader fallback",
                        {
                            "session_id": session_id,
                            "run_id": run_id,
                            "message_seq_in_run": message_seq,
                            "event_type": event_type,
                        },
                    ),
                    *diagnostics,
                ),
            )

        key = ProjectionKey(session_id=session_id, run_id=run_id, message_seq_in_run=message_seq)
        existing = self._store.get_projected_message(key)
        conversation_message_id = _text(
            event.get("conversation_message_id")
            or event.get("conversationMessageId")
            or payload.get("conversation_message_id")
            or payload.get("conversationMessageId")
        ) or conversation_message_id_for(key)
        source_event_seq = _text(event.get("seq") or payload.get("seq"))
        metadata = {
            "session_id": session_id,
            "run_id": run_id,
            "turn_id": _text(event.get("turn_id") or event.get("turnId") or payload.get("turn_id") or payload.get("turnId")),
            "message_seq_in_run": message_seq,
            "participant_id": participant_id,
            "participantId": participant_id,
            "activity_kind": _text(event.get("activity_kind") or event.get("activityKind") or payload.get("activity_kind") or payload.get("activityKind")),
            "activity_id": _text(event.get("activity_id") or event.get("activityId") or payload.get("activity_id") or payload.get("activityId")),
            "source_event_seq": source_event_seq,
            "projection_version": "2026-06-28",
        }
        if legacy_identity:
            metadata["legacy_message_identity"] = True
        if existing is not None:
            metadata = {**existing.metadata, **{key: value for key, value in metadata.items() if value != ""}}

        existing_content = existing.content if existing else ""
        existing_reasoning = existing.reasoning if existing else ""

        if event_type == "message.start":
            content = existing.content if existing else ""
            reasoning = existing_reasoning
            status = "streaming"
        elif event_type == "message.delta":
            delta = _event_text(event)
            content = self._apply_delta(existing_content, delta, payload, diagnostics)
            reasoning = existing_reasoning
            status = existing.status if existing and existing.status != "streaming" else "streaming"
        elif event_type == "message.complete":
            final_text = _event_text(event, terminal=True)
            content = final_text if final_text else existing_content
            reasoning = existing_reasoning
            status = _event_status(event)
        else:
            content = existing_content
            reasoning_delta = _event_reasoning_text(event)
            reasoning = self._apply_reasoning_delta(
                existing_reasoning,
                reasoning_delta,
                payload,
                diagnostics,
            )
            status = existing.status if existing and existing.status != "streaming" else "streaming"

        message = ProjectedTranscriptMessage(
            conversation_message_id=conversation_message_id,
            session_id=session_id,
            role=_text(payload.get("role")) or "assistant",
            content=content,
            participant_id=participant_id,
            metadata=metadata,
            status=status,
            reasoning=reasoning,
        )
        stored = self._store.upsert_projected_message(message)
        return ProjectionResult(
            applied=True,
            action="created" if existing is None else "updated",
            message=stored,
            diagnostics=tuple(diagnostics),
        )

    def _message_seq(self, event: dict[str, Any]) -> tuple[str, bool]:
        payload = _payload(event)
        seq = _text(
            event.get("message_seq_in_run")
            or event.get("messageSeqInRun")
            or payload.get("message_seq_in_run")
            or payload.get("messageSeqInRun")
        )
        if seq:
            return seq, False
        source_seq = _text(event.get("seq") or payload.get("seq"))
        if source_seq and _text(event.get("type")) != "message.start":
            return f"legacy-source-seq:{source_seq}", True
        return "", False

    def _apply_delta(
        self,
        current: str,
        delta: str,
        payload: dict[str, Any],
        diagnostics: list[ProjectionDiagnostic],
    ) -> str:
        if not delta:
            return current
        mode = _text(payload.get("mode")).lower()
        if mode == "replace":
            return delta
        offset = payload.get("offset")
        if offset is None:
            return current + delta
        try:
            parsed_offset = int(offset)
        except (TypeError, ValueError):
            diagnostics.append(
                ProjectionDiagnostic(
                    "delta-offset-invalid",
                    "message delta offset is not an integer",
                    {"offset": offset},
                )
            )
            return current + delta
        if parsed_offset != len(current):
            diagnostics.append(
                ProjectionDiagnostic(
                    "delta-offset-diverged",
                    "message delta offset does not match projected content length",
                    {"offset": parsed_offset, "content_length": len(current)},
                )
            )
            return current + delta
        return current + delta

    def _apply_reasoning_delta(
        self,
        current: str,
        delta: str,
        payload: dict[str, Any],
        diagnostics: list[ProjectionDiagnostic],
    ) -> str:
        if not delta:
            return current
        mode = _text(payload.get("mode")).lower()
        if mode == "replace":
            return delta
        offset = payload.get("offset")
        if offset is not None:
            try:
                parsed_offset = int(offset)
            except (TypeError, ValueError):
                diagnostics.append(
                    ProjectionDiagnostic(
                        "reasoning-delta-offset-invalid",
                        "reasoning delta offset is not an integer",
                        {"offset": offset},
                    )
                )
                return current + delta
            if parsed_offset == len(current):
                return current + delta
            if 0 <= parsed_offset < len(current):
                already_projected = current[parsed_offset : parsed_offset + len(delta)]
                if already_projected == delta:
                    return current
                if parsed_offset == 0 and delta.startswith(current):
                    return delta
            diagnostics.append(
                ProjectionDiagnostic(
                    "reasoning-delta-offset-diverged",
                    "reasoning delta offset does not match projected reasoning length",
                    {"offset": parsed_offset, "reasoning_length": len(current)},
                )
            )
            return current + delta
        if delta == current:
            return current
        if current and delta.startswith(current):
            return delta
        return current + delta
