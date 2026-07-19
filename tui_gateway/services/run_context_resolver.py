"""Resolve canonical RunContext payloads for gateway-owned executions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.services.profile_context import profile_context_for_params


def _text(value: Any) -> str:
    return str(value or "").strip()


def _participants(db: Any, conversation_session_id: str) -> list[dict[str, Any]]:
    try:
        rows = db.participants.list_conversation_participants(conversation_session_id)
    except Exception:
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _participant_for_params(
    params: dict[str, Any],
    participants: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    explicit = _text(params.get("participant_id") or params.get("participantId"))
    if explicit:
        participant = next(
            (
                participant
                for participant in participants
                if _text(participant.get("participant_id")) == explicit
            ),
            {},
        )
        if participant:
            return explicit, participant
        if participants:
            raise ValueError(
                f"participant_id {explicit!r} is not in conversation participant roster"
            )
        return "", {}

    runtime_scope_key = _text(
        params.get("runtime_scope_key") or params.get("runtimeScopeKey")
    )
    agent_profile_id = _text(
        params.get("agent_profile_id") or params.get("agentProfileId")
    )
    execution_participants = [
        participant
        for participant in participants
        if _text(participant.get("role")).lower()
        in {"agent", "leader", "member", "team"}
    ]
    candidates = (
        [
            participant
            for participant in execution_participants
            if _text(participant.get("runtime_scope_key")) == runtime_scope_key
        ]
        if runtime_scope_key
        else []
    )
    if not candidates and agent_profile_id:
        candidates = [
            participant
            for participant in execution_participants
            if _text(participant.get("agent_profile_id")) == agent_profile_id
        ]
    participant_ids = {
        _text(candidate.get("participant_id"))
        for candidate in candidates
        if _text(candidate.get("participant_id"))
    }
    if len(participant_ids) != 1:
        return "", {}
    participant_id = next(iter(participant_ids))
    return participant_id, next(
        candidate
        for candidate in candidates
        if _text(candidate.get("participant_id")) == participant_id
    )


def _activity_id(
    *,
    conversation_session_id: str,
    participant_id: str,
    activity_kind: str,
    explicit: Any,
) -> str:
    activity_id = _text(explicit)
    if activity_id:
        return activity_id
    if activity_kind == "chat":
        return f"chat:{conversation_session_id}"
    if activity_kind in {"member_chat", "agent_dispatch", "team_dispatch"}:
        return f"act-{activity_kind}:{conversation_session_id}:{participant_id}"
    return ""


def _canonical_existing_context(
    payload: Any,
    *,
    conversation_session_id: str,
    params: dict[str, Any],
) -> RunContext:
    context = RunContext.from_payload(payload)
    expected = {
        "conversation_session_id": conversation_session_id,
        "participant_id": _text(
            params.get("participant_id") or params.get("participantId")
        ),
        "activity_id": _text(params.get("activity_id") or params.get("activityId")),
        "activity_kind": _text(
            params.get("activity_kind") or params.get("activityKind")
        ),
        "execution_scope_key": _text(
            params.get("runtime_scope_key") or params.get("runtimeScopeKey")
        ),
    }
    for field_name, expected_value in expected.items():
        if expected_value and _text(getattr(context, field_name)) != expected_value:
            raise ValueError(
                f"run_context_json {field_name} does not match run.submit {field_name}"
            )
    return context


def normalize_run_context_params(
    params: dict[str, Any],
    *,
    conversation_session_id: str,
    db: Any = None,
) -> dict[str, Any]:
    """Return params carrying one validated, owner-authoritative RunContext.

    Team runtimes already submit a complete payload.  Direct conversation
    callers may submit the public participant/activity/scope fields instead;
    Hermes resolves homes and participant metadata that belong to the runtime,
    rather than requiring the desktop client to know internal filesystem state.
    Calls without enough participant evidence remain unchanged so generic Hermes
    CLI integrations that do not use the conversation protocol keep working.
    """

    stable_conversation_id = _text(conversation_session_id)
    if not stable_conversation_id:
        return params
    existing = params.get("run_context_json") or params.get("runContextJson")
    if existing is not None:
        context = _canonical_existing_context(
            existing,
            conversation_session_id=stable_conversation_id,
            params=params,
        )
        roster = _participants(db, stable_conversation_id) if db is not None else []
        if roster:
            roster_participant = next(
                (
                    participant
                    for participant in roster
                    if _text(participant.get("participant_id"))
                    == context.participant_id
                ),
                {},
            )
            if not roster_participant:
                raise ValueError(
                    "run_context_json participant_id is not in conversation "
                    "participant roster"
                )
        return {
            **params,
            "run_context_json": context.to_payload(),
            "participant_id": context.participant_id,
            "activity_id": context.activity_id,
            "activity_kind": context.activity_kind,
            "runtime_scope_key": context.execution_scope_key,
        }

    roster = _participants(db, stable_conversation_id) if db is not None else []
    participant_id, participant = _participant_for_params(params, roster)
    if not participant_id:
        return params
    requested_scope_key = _text(
        params.get("runtime_scope_key") or params.get("runtimeScopeKey")
    )
    participant_scope_key = _text(participant.get("runtime_scope_key"))
    if (
        requested_scope_key
        and participant_scope_key
        and requested_scope_key != participant_scope_key
    ):
        raise ValueError(
            "runtime_scope_key does not match conversation participant roster"
        )
    execution_scope_key = requested_scope_key or participant_scope_key
    if not execution_scope_key:
        return params
    requested_profile_id = _text(
        params.get("agent_profile_id") or params.get("agentProfileId")
    )
    participant_profile_id = _text(participant.get("agent_profile_id"))
    if (
        requested_profile_id
        and participant_profile_id
        and requested_profile_id != participant_profile_id
    ):
        raise ValueError(
            "agent_profile_id does not match conversation participant roster"
        )
    participant_role = _text(participant.get("role")).lower()
    activity_kind = _text(
        params.get("activity_kind") or params.get("activityKind")
    ) or ("member_chat" if participant_role == "member" else "chat")
    activity_id = _activity_id(
        conversation_session_id=stable_conversation_id,
        participant_id=participant_id,
        activity_kind=activity_kind,
        explicit=params.get("activity_id") or params.get("activityId"),
    )
    profile = profile_context_for_params(params) or {}
    control_home = (
        Path(
            _text(os.environ.get("DOVIE_HERMES_CONTROL_HOME")) or str(get_hermes_home())
        )
        .expanduser()
        .resolve()
    )
    execution_home = (
        Path(_text(profile.get("hermes_home")) or str(control_home))
        .expanduser()
        .resolve()
    )
    context = RunContext(
        conversation_session_id=stable_conversation_id,
        participant_id=participant_id,
        activity_id=activity_id,
        activity_kind=activity_kind,
        execution_scope_key=execution_scope_key,
        control_home=str(control_home),
        execution_home=str(execution_home),
        profile_id=_text(
            participant_profile_id or profile.get("id") or requested_profile_id
        ),
        profile_version_id=_text(
            participant.get("agent_profile_version_id")
            or profile.get("agent_profile_version_id")
        ),
        memory_namespace=_text(participant.get("memory_namespace")),
    )
    return {
        **params,
        "run_context_json": context.to_payload(),
        "participant_id": context.participant_id,
        "activity_id": context.activity_id,
        "activity_kind": context.activity_kind,
        "runtime_scope_key": context.execution_scope_key,
    }


__all__ = ["normalize_run_context_params"]
