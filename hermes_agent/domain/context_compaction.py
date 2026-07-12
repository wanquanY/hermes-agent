"""Scope-aware structured compaction for participant and activity contexts.

The ordinary ContextEngine decides *when* a model context must be compacted.
This module owns the team-specific projection that is persisted after that
boundary.  It never mutates the canonical transcript and never promotes a
summary into long-term conversation memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from hermes_agent.domain.actor_summary_compiler import compile_actor_context_summary


def _text(value: Any) -> str:
    return str(value or "").strip()


def _event_id(message: Mapping[str, Any]) -> str:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return _text(
        message.get("message_id")
        or message.get("event_id")
        or metadata.get("event_id")
        or metadata.get("message_id")
        or metadata.get("persist_message_key")
    )


def _message_seq(message: Mapping[str, Any]) -> int:
    for value in (
        message.get("message_id"),
        message.get("seq"),
        (message.get("metadata") or {}).get("seq")
        if isinstance(message.get("metadata"), dict)
        else None,
    ):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


@dataclass(frozen=True)
class ContextScope:
    conversation_session_id: str
    actor_participant_id: str
    activity_id: str = ""
    activity_kind: str = "chat"
    node_id: str = ""
    attempt_id: str = ""
    snapshot_id: str = ""
    activity_snapshot_id: str = ""
    conversation_revision: int = 0
    transcript_cursor: int = 0
    participant_memory_revision: int = 0

    @classmethod
    def from_run_context(cls, run_context: Any) -> "ContextScope":
        return cls(
            conversation_session_id=_text(getattr(run_context, "conversation_session_id", "")),
            actor_participant_id=_text(getattr(run_context, "participant_id", "")),
            activity_id=_text(getattr(run_context, "activity_id", "")),
            activity_kind=_text(getattr(run_context, "activity_kind", "")) or "chat",
            node_id=_text(getattr(run_context, "node_id", "")),
            attempt_id=_text(getattr(run_context, "attempt_id", "")),
            snapshot_id=_text(getattr(run_context, "context_snapshot_id", "")),
            activity_snapshot_id=_text(
                getattr(run_context, "activity_context_snapshot_id", "")
            ),
            conversation_revision=max(
                0, int(getattr(run_context, "conversation_revision", 0) or 0)
            ),
            transcript_cursor=max(
                0, int(getattr(run_context, "transcript_cursor", 0) or 0)
            ),
            participant_memory_revision=max(
                0, int(getattr(run_context, "participant_memory_revision", 0) or 0)
            ),
        )

    @property
    def kind(self) -> str:
        if self.node_id:
            return "node"
        if self.activity_kind in {"mission", "team_dispatch"} or self.activity_id.startswith(
            "mission:"
        ):
            return "activity"
        return "actor"

    @property
    def lease_key(self) -> str:
        parts = [self.kind, self.conversation_session_id, self.actor_participant_id]
        if self.kind in {"activity", "node"}:
            parts.append(self.activity_id)
        if self.kind == "node":
            parts.extend([self.node_id, self.attempt_id])
        return ":".join(parts)

    @property
    def summary_snapshot_id(self) -> str:
        if self.kind in {"activity", "node"}:
            return self.activity_snapshot_id or self.snapshot_id
        return self.snapshot_id


def _merge_records(
    previous: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for raw in [*previous, *current]:
        if not isinstance(raw, Mapping):
            continue
        record = dict(raw)
        sources = [str(item) for item in record.get("source_event_ids") or [] if str(item)]
        key = sources[0] if sources else f"{record.get('participant_id')}:{record.get('role')}:{record.get('excerpt')}"
        if key in positions:
            merged[positions[key]] = record
        else:
            positions[key] = len(merged)
            merged.append(record)
    return merged[-limit:]


def merge_actor_summaries(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge an incremental actor projection without losing provenance."""
    old = previous if isinstance(previous, Mapping) else {}
    result = dict(current)
    for key in ("user_requirements", "actor_statements"):
        result[key] = _merge_records(old.get(key) or [], current.get(key) or [])

    by_participant: dict[str, list[Mapping[str, Any]]] = {}
    for source in (old, current):
        for group in source.get("other_participant_statements") or []:
            if not isinstance(group, Mapping):
                continue
            participant_id = _text(group.get("participant_id")) or "unknown"
            by_participant.setdefault(participant_id, []).extend(group.get("statements") or [])
    other_groups: list[dict[str, Any]] = []
    for participant_id, statements in sorted(by_participant.items()):
        records = _merge_records([], statements)
        other_groups.append(
            {
                "participant_id": participant_id,
                "statements": records,
                "source_event_ids": [
                    event_id
                    for record in records
                    for event_id in record.get("source_event_ids") or []
                ],
            }
        )
    result["other_participant_statements"] = other_groups
    result["source_event_ids"] = list(
        dict.fromkeys([*(old.get("source_event_ids") or []), *(current.get("source_event_ids") or [])])
    )
    return result


def merge_structured_summaries(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge Activity/Node checkpoints deterministically after a CAS retry."""
    old = previous if isinstance(previous, Mapping) else {}
    merged = dict(old)
    merged.update({key: value for key, value in current.items() if not isinstance(value, list)})
    for key in set(old).union(current):
        old_value = old.get(key)
        current_value = current.get(key)
        if not isinstance(old_value, list) and not isinstance(current_value, list):
            continue
        values: list[Any] = []
        seen: set[str] = set()
        for value in [*(old_value or []), *(current_value or [])]:
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            values.append(value)
        merged[key] = values[-50:]
    return merged


def compile_activity_context_summary(
    messages: Sequence[Mapping[str, Any]],
    *,
    scope: ContextScope,
) -> dict[str, Any]:
    actor_projection = compile_actor_context_summary(
        [dict(message) for message in messages if isinstance(message, Mapping)],
        actor_participant_id=scope.actor_participant_id,
    )
    return {
        "activity_id": scope.activity_id,
        "objective_and_constraints": actor_projection.get("user_requirements") or [],
        "accepted_handoffs": [],
        "progress": actor_projection.get("actor_statements") or [],
        "decisions": [],
        "open_questions": [],
        "artifact_refs": [],
        "node_statuses": [],
        "source_memory_ids": [],
        "source_event_ids": actor_projection.get("source_event_ids") or [],
    }


def compile_node_attempt_summary(
    messages: Sequence[Mapping[str, Any]],
    *,
    scope: ContextScope,
) -> dict[str, Any]:
    projection = compile_actor_context_summary(
        [dict(message) for message in messages if isinstance(message, Mapping)],
        actor_participant_id=scope.actor_participant_id,
    )
    return {
        "activity_id": scope.activity_id,
        "node_id": scope.node_id,
        "attempt_id": scope.attempt_id,
        "task_brief_ref": scope.activity_snapshot_id or scope.snapshot_id,
        "completed_work": projection.get("actor_statements") or [],
        "tool_facts": [],
        "produced_artifacts": [],
        "verification_performed": [],
        "blockers": [],
        "source_event_ids": projection.get("source_event_ids") or [],
    }


class ContextCompactionService:
    """Persist a structured summary after the ordinary ContextEngine wins."""

    def __init__(self, memory_service: Any) -> None:
        self._memory = memory_service

    def checkpoint(self, scope: ContextScope, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not scope.conversation_session_id or not scope.summary_snapshot_id:
            return {}
        holder = self._memory.try_acquire_compaction_lease(scope.lease_key)
        if not holder:
            return {}
        try:
            if scope.kind == "actor":
                return self._checkpoint_actor(scope, messages)
            if scope.kind == "activity":
                summary = compile_activity_context_summary(messages, scope=scope)
                for _ in range(3):
                    previous = self._memory.latest_activity_summary(scope.activity_id)
                    merged = merge_structured_summaries(previous.get("summary"), summary)
                    try:
                        return self._memory.commit_activity_summary(
                            conversation_session_id=scope.conversation_session_id,
                            activity_id=scope.activity_id,
                            snapshot_id=scope.summary_snapshot_id,
                            expected_revision=int(previous.get("revision") or 0),
                            activity_context_revision=scope.conversation_revision,
                            summary=merged,
                            source_event_ids=merged.get("source_event_ids") or [],
                            source_memory_ids=merged.get("source_memory_ids") or [],
                        )
                    except RuntimeError:
                        continue
                raise RuntimeError("activity context summary revision conflict")
            summary = compile_node_attempt_summary(messages, scope=scope)
            attempt_id = scope.attempt_id or "attempt:1"
            for _ in range(3):
                previous = self._memory.latest_node_attempt_summary(
                    scope.activity_id, scope.node_id, attempt_id
                )
                merged = merge_structured_summaries(previous.get("summary"), summary)
                try:
                    return self._memory.commit_node_attempt_summary(
                        conversation_session_id=scope.conversation_session_id,
                        activity_id=scope.activity_id,
                        node_id=scope.node_id,
                        attempt_id=attempt_id,
                        snapshot_id=scope.summary_snapshot_id,
                        expected_revision=int(previous.get("revision") or 0),
                        node_attempt_revision=scope.conversation_revision,
                        summary=merged,
                        source_event_ids=merged.get("source_event_ids") or [],
                    )
                except RuntimeError:
                    continue
            raise RuntimeError("node attempt summary revision conflict")
        finally:
            self._memory.release_compaction_lease(scope.lease_key, holder)

    def _checkpoint_actor(
        self, scope: ContextScope, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        for _ in range(3):
            state = self._memory.actor_compaction_state(
                scope.conversation_session_id, scope.actor_participant_id
            )
            cursor = int(state.get("transcript_cursor") or 0)
            memory_revision = int(state.get("memory_revision") or 0)
            previous_row = self._memory.latest_actor_summary(
                scope.conversation_session_id, scope.actor_participant_id
            )
            previous = previous_row.get("summary") if isinstance(previous_row, dict) else {}
            incremental = [
                message
                for message in messages
                if not _message_seq(message) or _message_seq(message) > cursor
            ]
            if not incremental and previous_row:
                return previous_row
            compiled = compile_actor_context_summary(
                incremental, actor_participant_id=scope.actor_participant_id
            )
            summary = merge_actor_summaries(previous, compiled)
            max_seq = max([cursor, *[_message_seq(item) for item in messages]])
            try:
                return self._memory.commit_actor_summary(
                    conversation_session_id=scope.conversation_session_id,
                    actor_participant_id=scope.actor_participant_id,
                    snapshot_id=scope.summary_snapshot_id,
                    expected_memory_revision=memory_revision,
                    from_seq=cursor + 1,
                    to_seq=max(cursor, max_seq, scope.conversation_revision),
                    conversation_revision=max(scope.conversation_revision, max_seq),
                    summary=summary,
                    source_event_ids=summary.get("source_event_ids") or [],
                )
            except RuntimeError:
                continue
        raise RuntimeError("participant actor summary revision conflict")


__all__ = [
    "ContextCompactionService",
    "ContextScope",
    "compile_activity_context_summary",
    "compile_node_attempt_summary",
    "merge_actor_summaries",
    "merge_structured_summaries",
]
