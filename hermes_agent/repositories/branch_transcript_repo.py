"""Atomic transcript materialization for user-created conversation branches."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from hermes_agent.domain.message_owner_projection import (
    project_render_message_owners,
)
from hermes_agent.repositories.conversation_participant_repo import (
    participant_memory_namespace,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_record(value: Any) -> dict[str, Any]:
    try:
        return _record(json.loads(str(value or "{}")))
    except (TypeError, ValueError):
        return {}


def _message_run_ids(metadata: dict[str, Any]) -> set[str]:
    team_mission = _record(metadata.get("team_mission") or metadata.get("teamMission"))
    return {
        stable
        for value in (
            metadata.get("run_id"),
            metadata.get("runId"),
            metadata.get("source_run_id"),
            metadata.get("sourceRunId"),
            team_mission.get("source_run_id"),
            team_mission.get("sourceRunId"),
        )
        if (stable := _text(value))
    }


def _participant_role(message_role: str, participant_id: str) -> str:
    if participant_id.startswith("leader:"):
        return "leader"
    if participant_id.startswith("member:"):
        return "member"
    if message_role == "user" or participant_id == "user":
        return "user"
    if message_role in {"system", "session_meta"} or participant_id == "system":
        return "system"
    return "agent"


class BranchTranscriptRepo:
    """Build a self-contained child transcript from a source lineage prefix.

    Branches do not replay the parent's run ledger. Every copied row therefore
    receives a durable owner before insertion, and the child receives its own
    participant roster. This keeps render correctness independent of source
    run-event retention and avoids sharing participant memory namespaces.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def materialize_prefix(
        self,
        source_session_ids: list[str],
        included_row_id: int,
        target_session_id: str,
        copy_started_at: float,
    ) -> int:
        sources = [stable for value in source_session_ids if (stable := _text(value))]
        target = _text(target_session_id)
        row_limit = int(included_row_id or 0)
        if not sources:
            raise ValueError("source_session_ids are required")
        if not target:
            raise ValueError("target_session_id is required")
        if row_limit <= 0:
            raise ValueError("included_row_id must be positive")

        placeholders = ",".join("?" for _ in sources)
        rows = self._conn.execute(
            f"""
            SELECT
                id, session_id, role, content, participant_id, tool_call_id,
                tool_calls, tool_name, token_count, finish_reason, reasoning,
                reasoning_content, reasoning_details, codex_reasoning_items,
                codex_message_items, platform_message_id,
                conversation_message_id, metadata_json, api_content
              FROM messages
             WHERE session_id IN ({placeholders})
               AND id <= ?
             ORDER BY id
            """,
            (*sources, row_limit),
        ).fetchall()
        if not rows:
            return 0

        messages: list[dict[str, Any]] = []
        referenced_run_ids: set[str] = set()
        message_identities: set[str] = set()
        for row in rows:
            metadata = _json_record(row["metadata_json"])
            referenced_run_ids.update(_message_run_ids(metadata))
            message_identities.add(str(row["id"]))
            conversation_message_id = _text(row["conversation_message_id"])
            if conversation_message_id:
                message_identities.add(conversation_message_id)
            messages.append({
                "id": int(row["id"]),
                "message_id": str(row["id"]),
                "conversation_message_id": conversation_message_id,
                "role": _text(row["role"]),
                "participant_id": _text(row["participant_id"]),
                "metadata": metadata,
            })

        participants = self._source_participants(sources)
        if self._source_lineage_is_direct(sources) and not any(
            _text(participant.get("role")) in {"agent", "leader", "member", "team"}
            for participant in participants
        ):
            participants.append({
                "participant_id": "agent",
                "role": "agent",
            })
        run_events = self._source_run_events(
            sources,
            referenced_run_ids,
            message_identities,
        )
        projected = project_render_message_owners(
            messages,
            run_events=run_events,
            participants=participants,
            allow_single_execution_participant=True,
        )
        projected_by_id = {int(message["id"]): message for message in projected}

        started_at = float(copy_started_at or time.time())
        insert_rows: list[tuple[Any, ...]] = []
        for index, row in enumerate(rows, start=1):
            owner = projected_by_id[int(row["id"])]
            metadata = _record(owner.get("metadata"))
            metadata["branch_materialization"] = {
                "version": 1,
                "source_session_id": _text(row["session_id"]),
                "source_message_id": str(row["id"]),
            }
            insert_rows.append((
                target,
                row["role"],
                row["content"],
                _text(owner.get("participant_id")),
                row["tool_call_id"],
                row["tool_calls"],
                row["tool_name"],
                started_at + (index * 0.000001),
                row["token_count"],
                row["finish_reason"],
                row["reasoning"],
                row["reasoning_content"],
                row["reasoning_details"],
                row["codex_reasoning_items"],
                row["codex_message_items"],
                row["platform_message_id"],
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                row["api_content"],
            ))
        self._conn.executemany(
            """
            INSERT INTO messages (
                session_id, role, content, participant_id, tool_call_id,
                tool_calls, tool_name, timestamp, token_count, finish_reason,
                reasoning, reasoning_content, reasoning_details,
                codex_reasoning_items, codex_message_items,
                platform_message_id, metadata_json, api_content
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_rows,
        )
        self._materialize_participants(
            target_session_id=target,
            source_participants=participants,
            projected_messages=projected,
            created_at=started_at,
        )
        return len(insert_rows)

    def _source_participants(
        self,
        source_session_ids: list[str],
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in source_session_ids)
        rows = self._conn.execute(
            f"""
            SELECT *
              FROM conversation_participants
             WHERE conversation_session_id IN ({placeholders})
            """,
            tuple(source_session_ids),
        ).fetchall()
        source_rank = {
            session_id: index for index, session_id in enumerate(source_session_ids)
        }
        ordered = sorted(
            (dict(row) for row in rows),
            key=lambda row: (
                source_rank.get(_text(row.get("conversation_session_id")), -1),
                float(row.get("updated_at") or 0),
            ),
        )
        by_id: dict[str, dict[str, Any]] = {}
        for row in ordered:
            participant_id = _text(row.get("participant_id"))
            if participant_id:
                by_id[participant_id] = row
        return list(by_id.values())

    def _source_lineage_is_direct(self, source_session_ids: list[str]) -> bool:
        placeholders = ",".join("?" for _ in source_session_ids)
        rows = self._conn.execute(
            f"""
            SELECT conversation_kind
              FROM sessions
             WHERE id IN ({placeholders})
            """,
            tuple(source_session_ids),
        ).fetchall()
        return bool(rows) and all(
            _text(row["conversation_kind"]) == "direct" for row in rows
        )

    def _source_run_events(
        self,
        source_session_ids: list[str],
        run_ids: set[str],
        message_ids: set[str],
    ) -> list[dict[str, Any]]:
        if not run_ids and not message_ids:
            return []
        event_rows: dict[int, sqlite3.Row] = {}
        session_placeholders = ",".join("?" for _ in source_session_ids)
        ordered_run_ids = sorted(run_ids)
        for offset in range(0, len(ordered_run_ids), 400):
            chunk = ordered_run_ids[offset : offset + 400]
            run_placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""
                SELECT id, run_id, seq, participant_id, payload_json, event_json
                  FROM run_events
                 WHERE session_id IN ({session_placeholders})
                   AND run_id IN ({run_placeholders})
                   AND TRIM(COALESCE(participant_id, '')) <> ''
                 ORDER BY seq, id
                """,
                (*source_session_ids, *chunk),
            ).fetchall()
            event_rows.update({int(row["id"]): row for row in rows})
        ordered_message_ids = sorted(message_ids)
        for offset in range(0, len(ordered_message_ids), 300):
            chunk = ordered_message_ids[offset : offset + 300]
            message_placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""
                SELECT id, run_id, seq, participant_id, payload_json, event_json
                  FROM run_events
                 WHERE session_id IN ({session_placeholders})
                   AND TRIM(COALESCE(participant_id, '')) <> ''
                   AND (
                        projected_message_id IN ({message_placeholders})
                        OR CASE WHEN json_valid(payload_json)
                           THEN CAST(json_extract(payload_json, '$.message_id') AS TEXT)
                           ELSE '' END IN ({message_placeholders})
                        OR CASE WHEN json_valid(payload_json)
                           THEN CAST(json_extract(payload_json, '$.messageId') AS TEXT)
                           ELSE '' END IN ({message_placeholders})
                   )
                 ORDER BY seq, id
                """,
                (
                    *source_session_ids,
                    *chunk,
                    *chunk,
                    *chunk,
                ),
            ).fetchall()
            event_rows.update({int(row["id"]): row for row in rows})
        events: list[dict[str, Any]] = []
        for row in sorted(
            event_rows.values(),
            key=lambda value: (int(value["seq"] or 0), int(value["id"])),
        ):
            event = _json_record(row["event_json"])
            event.update({
                "run_id": _text(row["run_id"]),
                "seq": int(row["seq"] or 0),
                "participant_id": _text(row["participant_id"]),
                "payload": _json_record(row["payload_json"]),
            })
            events.append(event)
        return events

    def _materialize_participants(
        self,
        *,
        target_session_id: str,
        source_participants: list[dict[str, Any]],
        projected_messages: list[dict[str, Any]],
        created_at: float,
    ) -> None:
        participants = {
            _text(row.get("participant_id")): dict(row)
            for row in source_participants
            if _text(row.get("participant_id"))
        }
        for message in projected_messages:
            participant_id = _text(message.get("participant_id"))
            if not participant_id or participant_id in participants:
                continue
            participants[participant_id] = {
                "participant_id": participant_id,
                "role": _participant_role(_text(message.get("role")), participant_id),
            }
        if not participants:
            return
        rows = []
        for participant_id, participant in participants.items():
            rows.append((
                target_session_id,
                participant_id,
                _text(participant.get("role")) or "agent",
                _text(participant.get("member_id")),
                _text(participant.get("agent_profile_id")),
                _text(participant.get("agent_profile_version_id")),
                _text(participant.get("runtime_scope_key")),
                participant_memory_namespace(target_session_id, participant_id),
                _text(participant.get("status")) or "active",
                _text(participant.get("display_name")),
                _text(participant.get("avatar")),
                _text(participant.get("metadata_json")),
                created_at,
                created_at,
            ))
        self._conn.executemany(
            """
            INSERT INTO conversation_participants (
                conversation_session_id, participant_id, role, member_id,
                agent_profile_id, agent_profile_version_id,
                runtime_scope_key, memory_namespace, status,
                display_name, avatar, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


__all__ = ["BranchTranscriptRepo"]
