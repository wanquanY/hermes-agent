"""Canonical participant ownership for persisted and render-ready messages.

Persisted messages created before participant-aware storage may not carry a
``participant_id`` even though the conversation roster and run ledger retain
enough evidence to recover it.  This module owns that recovery policy so every
conversation renderer uses the same deterministic rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class MessageOwnerResolutionError(ValueError):
    """Raised when a render-ready message has no authoritative participant."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _metadata(message: dict[str, Any]) -> dict[str, Any]:
    direct = _record(message.get("metadata"))
    serialized = message.get("metadata_json") or message.get("metadataJson")
    if not serialized:
        return direct
    try:
        parsed = json.loads(str(serialized))
    except (TypeError, ValueError):
        parsed = {}
    return {**_record(parsed), **direct}


def message_participant_id(message: dict[str, Any]) -> str:
    """Return participant identity already carried by a message envelope."""

    metadata = _metadata(message)
    team_mission = _record(
        message.get("teamMission")
        or message.get("team_mission")
        or metadata.get("team_mission")
        or metadata.get("teamMission")
    )
    return _text(
        message.get("participant_id")
        or message.get("participantId")
        or metadata.get("participant_id")
        or metadata.get("participantId")
        or team_mission.get("participant_id")
        or team_mission.get("participantId")
    )


def _event_participant_id(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    payload = _record(event.get("payload"))
    return _text(
        event.get("participant_id")
        or event.get("participantId")
        or payload.get("participant_id")
        or payload.get("participantId")
    )


def _event_run_id(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    payload = _record(event.get("payload"))
    return _text(event.get("run_id") or payload.get("run_id") or payload.get("runId"))


def _message_id(message: dict[str, Any]) -> str:
    return _text(
        message.get("conversation_message_id")
        or message.get("conversationMessageId")
        or message.get("message_id")
        or message.get("messageId")
        or message.get("id")
    )


def _message_source_run_id(message: dict[str, Any]) -> str:
    metadata = _metadata(message)
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    return _text(
        metadata.get("source_run_id")
        or metadata.get("sourceRunId")
        or team_mission.get("source_run_id")
        or team_mission.get("sourceRunId")
    )


def _message_source_seq(message: dict[str, Any]) -> str:
    metadata = _metadata(message)
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    return _text(
        metadata.get("source_seq")
        or metadata.get("sourceSeq")
        or team_mission.get("source_seq")
        or team_mission.get("sourceSeq")
    )


def run_event_participant_index(run_events: list[Any]) -> dict[str, tuple[str, ...]]:
    """Index authoritative event owners by stable message/run identities."""

    indexed: dict[str, set[str]] = {}
    for event in run_events:
        if not isinstance(event, dict):
            continue
        participant_id = _event_participant_id(event)
        if not participant_id:
            continue
        payload = _record(event.get("payload"))
        run_id = _event_run_id(event)
        seq = _text(event.get("seq") or payload.get("seq"))
        source_seq = _text(
            event.get("source_seq")
            or event.get("sourceSeq")
            or payload.get("source_seq")
            or payload.get("sourceSeq")
        )
        message_id = _text(payload.get("message_id") or payload.get("messageId"))
        for key in (
            f"run:{run_id}" if run_id else "",
            f"run-seq:{run_id}:{seq}" if run_id and seq else "",
            f"run-seq:{run_id}:{source_seq}" if run_id and source_seq else "",
            f"message:{message_id}" if message_id else "",
        ):
            if key:
                indexed.setdefault(key, set()).add(participant_id)
    return {
        key: tuple(sorted(participant_ids)) for key, participant_ids in indexed.items()
    }


def participant_id_for_message_from_events(
    message: dict[str, Any],
    event_participants: dict[str, tuple[str, ...]],
) -> str:
    """Resolve a message owner from its persisted run/message correlation."""

    message_id = _message_id(message)
    metadata = _metadata(message)
    run_id = _text(metadata.get("run_id") or metadata.get("runId"))
    source_run_id = _message_source_run_id(message)
    source_seq = _message_source_seq(message)
    for key in (
        f"message:{message_id}" if message_id else "",
        f"run-seq:{source_run_id}:{source_seq}" if source_run_id and source_seq else "",
        f"run-seq:{run_id}:{source_seq}" if run_id and source_seq else "",
        f"run:{source_run_id}" if source_run_id else "",
        f"run:{run_id}" if run_id else "",
    ):
        participant_id = _single(event_participants.get(key)) if key else ""
        if participant_id:
            return participant_id
    return ""


def with_message_participant_id(
    message: dict[str, Any],
    participant_id: str,
) -> dict[str, Any]:
    """Return a message carrying the canonical owner in all supported views."""

    stable_participant_id = _text(participant_id) or message_participant_id(message)
    if not stable_participant_id:
        return message
    next_message = dict(message)
    next_message["participant_id"] = stable_participant_id
    next_message["participantId"] = stable_participant_id
    metadata = _metadata(next_message)
    metadata["participant_id"] = stable_participant_id
    metadata["participantId"] = stable_participant_id
    team_mission = _record(
        next_message.get("teamMission")
        or next_message.get("team_mission")
        or metadata.get("team_mission")
        or metadata.get("teamMission")
    )
    if team_mission:
        team_mission["participant_id"] = stable_participant_id
        team_mission["participantId"] = stable_participant_id
        metadata["team_mission"] = team_mission
        metadata["teamMission"] = team_mission
        next_message["team_mission"] = team_mission
        next_message["teamMission"] = team_mission
    next_message["metadata"] = metadata
    return next_message


@dataclass(frozen=True)
class _ParticipantRoster:
    by_role: dict[str, tuple[str, ...]]
    by_scope: dict[str, tuple[str, ...]]
    execution_participants: tuple[str, ...]


def _participant_roster(participants: list[Any]) -> _ParticipantRoster:
    roles: dict[str, set[str]] = {}
    scopes: dict[str, set[str]] = {}
    execution: set[str] = set()
    for raw in participants:
        if not isinstance(raw, dict):
            continue
        participant_id = _text(raw.get("participant_id") or raw.get("participantId"))
        if not participant_id:
            continue
        role = _text(raw.get("role")).lower()
        if role:
            roles.setdefault(role, set()).add(participant_id)
        runtime_scope_key = _text(
            raw.get("runtime_scope_key") or raw.get("runtimeScopeKey")
        )
        if runtime_scope_key:
            scopes.setdefault(runtime_scope_key, set()).add(participant_id)
        if role in {"agent", "leader", "member", "team"}:
            execution.add(participant_id)
    return _ParticipantRoster(
        by_role={role: tuple(sorted(values)) for role, values in roles.items()},
        by_scope={scope: tuple(sorted(values)) for scope, values in scopes.items()},
        execution_participants=tuple(sorted(execution)),
    )


def _single(values: tuple[str, ...] | None) -> str:
    return values[0] if values and len(values) == 1 else ""


def _message_runtime_scope_key(message: dict[str, Any]) -> str:
    metadata = _metadata(message)
    run_context = _record(metadata.get("run_context") or metadata.get("runContext"))
    return _text(
        metadata.get("runtime_scope_key")
        or metadata.get("runtimeScopeKey")
        or metadata.get("execution_scope_key")
        or metadata.get("executionScopeKey")
        or run_context.get("execution_scope_key")
        or run_context.get("executionScopeKey")
    )


def _resolve_message_participant_id(
    message: dict[str, Any],
    *,
    event_participants: dict[str, tuple[str, ...]],
    roster: _ParticipantRoster,
    allow_single_execution_participant: bool,
) -> str:
    carried = message_participant_id(message)
    if carried:
        return carried

    role = _text(message.get("role")).lower()
    if role == "user":
        user_participants = roster.by_role.get("user")
        return _single(user_participants) if user_participants else "user"
    if role in {"system", "session_meta"}:
        system_participants = roster.by_role.get("system")
        return _single(system_participants) if system_participants else "system"

    from_event = participant_id_for_message_from_events(message, event_participants)
    if from_event:
        return from_event

    runtime_scope_key = _message_runtime_scope_key(message)
    if runtime_scope_key:
        from_scope = _single(roster.by_scope.get(runtime_scope_key))
        if from_scope:
            return from_scope
    if allow_single_execution_participant:
        return _single(roster.execution_participants)
    return ""


def project_render_message_owners(
    messages: list[Any],
    *,
    run_events: list[Any],
    participants: list[Any],
    allow_single_execution_participant: bool,
) -> list[dict[str, Any]]:
    """Project every transcript row to a non-empty canonical participant id.

    A single-execution-participant fallback is safe whenever the persisted
    roster itself proves there is exactly one possible owner. Multi-participant
    team conversations never guess: they still require a carried owner, an
    authoritative run-event match, or a unique runtime-scope match.
    """

    event_participants = run_event_participant_index(run_events)
    roster = _participant_roster(participants)
    projected: list[dict[str, Any]] = []
    for index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            raise MessageOwnerResolutionError(f"messages[{index}] must be an object")
        participant_id = _resolve_message_participant_id(
            raw,
            event_participants=event_participants,
            roster=roster,
            allow_single_execution_participant=allow_single_execution_participant,
        )
        if not participant_id:
            raise MessageOwnerResolutionError(
                "message owner identity unavailable "
                f"at messages[{index}] role={_text(raw.get('role')) or 'unknown'} "
                f"message_id={_message_id(raw) or 'unknown'}"
            )
        projected.append(with_message_participant_id(raw, participant_id))
    return projected


__all__ = [
    "MessageOwnerResolutionError",
    "message_participant_id",
    "participant_id_for_message_from_events",
    "project_render_message_owners",
    "run_event_participant_index",
    "with_message_participant_id",
]
