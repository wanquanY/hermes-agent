"""Backfill canonical participant owners for visible transcript messages."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


version = 55
description = "backfill canonical participant owners for visible messages"


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return (
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _metadata(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return _record(parsed)


def _single(values: set[str] | None) -> str:
    return next(iter(values)) if values and len(values) == 1 else ""


def _participants(
    cursor: sqlite3.Cursor,
    session_id: str,
) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    by_role: dict[str, set[str]] = {}
    by_scope: dict[str, set[str]] = {}
    execution: set[str] = set()
    rows = cursor.execute(
        "SELECT participant_id, role, runtime_scope_key "
        "FROM conversation_participants WHERE conversation_session_id = ?",
        (session_id,),
    ).fetchall()
    for row in rows:
        participant_id = _text(row["participant_id"])
        if not participant_id:
            continue
        role = _text(row["role"]).lower()
        runtime_scope_key = _text(row["runtime_scope_key"])
        if role:
            by_role.setdefault(role, set()).add(participant_id)
        if runtime_scope_key:
            by_scope.setdefault(runtime_scope_key, set()).add(participant_id)
        if role in {"agent", "leader", "member", "team"}:
            execution.add(participant_id)
    return by_role, by_scope, execution


def _run_participants(
    cursor: sqlite3.Cursor,
    session_id: str,
) -> dict[str, set[str]]:
    indexed: dict[str, set[str]] = {}
    rows = cursor.execute(
        "SELECT run_id, participant_id FROM run_events "
        "WHERE session_id = ? AND TRIM(COALESCE(participant_id, '')) <> ''",
        (session_id,),
    ).fetchall()
    for row in rows:
        run_id = _text(row["run_id"])
        participant_id = _text(row["participant_id"])
        if run_id and participant_id:
            indexed.setdefault(run_id, set()).add(participant_id)
    return indexed


def _embedded_participant_id(metadata: dict[str, Any]) -> str:
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    return _text(
        metadata.get("participant_id")
        or metadata.get("participantId")
        or team_mission.get("participant_id")
        or team_mission.get("participantId")
    )


def _run_ids(metadata: dict[str, Any]) -> tuple[str, ...]:
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    values = (
        metadata.get("source_run_id"),
        metadata.get("sourceRunId"),
        team_mission.get("source_run_id"),
        team_mission.get("sourceRunId"),
        metadata.get("run_id"),
        metadata.get("runId"),
    )
    return tuple(dict.fromkeys(_text(value) for value in values if _text(value)))


def _runtime_scope_key(metadata: dict[str, Any]) -> str:
    run_context = _record(metadata.get("run_context") or metadata.get("runContext"))
    return _text(
        metadata.get("runtime_scope_key")
        or metadata.get("runtimeScopeKey")
        or metadata.get("execution_scope_key")
        or metadata.get("executionScopeKey")
        or run_context.get("execution_scope_key")
        or run_context.get("executionScopeKey")
    )


def _resolve_participant_id(
    *,
    role: str,
    metadata: dict[str, Any],
    by_role: dict[str, set[str]],
    by_scope: dict[str, set[str]],
    execution: set[str],
    run_participants: dict[str, set[str]],
) -> str:
    carried = _embedded_participant_id(metadata)
    if carried:
        return carried
    if role == "user":
        user_participants = by_role.get("user")
        return _single(user_participants) if user_participants else "user"
    if role in {"system", "session_meta"}:
        system_participants = by_role.get("system")
        return _single(system_participants) if system_participants else "system"
    for run_id in _run_ids(metadata):
        participant_id = _single(run_participants.get(run_id))
        if participant_id:
            return participant_id
    runtime_scope_key = _runtime_scope_key(metadata)
    if runtime_scope_key:
        participant_id = _single(by_scope.get(runtime_scope_key))
        if participant_id:
            return participant_id
    return _single(execution)


def apply(cursor: sqlite3.Cursor) -> None:
    required_tables = {
        "messages",
        "sessions",
        "conversation_participants",
        "run_events",
    }
    if not all(_table_exists(cursor, table) for table in required_tables):
        return
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(
        "SELECT m.id, m.session_id, m.role, m.metadata_json, "
        "s.conversation_kind "
        "FROM messages m JOIN sessions s ON s.id = m.session_id "
        "WHERE trim(m.participant_id) = '' "
        "AND s.conversation_kind IN ('direct', 'team')"
    ).fetchall()
    participants_by_session: dict[
        str,
        tuple[dict[str, set[str]], dict[str, set[str]], set[str]],
    ] = {}
    run_participants_by_session: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        session_id = _text(row["session_id"])
        if session_id not in participants_by_session:
            participants_by_session[session_id] = _participants(cursor, session_id)
        if session_id not in run_participants_by_session:
            run_participants_by_session[session_id] = _run_participants(
                cursor,
                session_id,
            )
        roster = participants_by_session[session_id]
        run_participants = run_participants_by_session[session_id]
        metadata = _metadata(row["metadata_json"])
        participant_id = _resolve_participant_id(
            role=_text(row["role"]).lower(),
            metadata=metadata,
            by_role=roster[0],
            by_scope=roster[1],
            execution=roster[2],
            run_participants=run_participants,
        )
        if not participant_id:
            continue
        metadata["participant_id"] = participant_id
        metadata["participantId"] = participant_id
        metadata["message_owner_migration"] = {"version": version}
        cursor.execute(
            "UPDATE messages SET participant_id = ?, metadata_json = ? WHERE id = ?",
            (
                participant_id,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                int(row["id"]),
            ),
        )


__all__ = ["apply", "description", "version"]
