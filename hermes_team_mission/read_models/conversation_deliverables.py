"""Conversation-level deliverable projection for Team Mission reads."""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_team_mission.context.conversation_projection import (
    dedupe_artifact_refs,
    final_deliverable_from_message,
    final_deliverable_with_artifact_refs,
    message_summary_from_message,
    message_with_deliverable_artifact_refs,
)
from hermes_team_mission.domain.utils import MEMORY_COMMITTED_STATUS
from hermes_team_mission.read_models.row_mapper import TeamMissionRowMapper


class ConversationDeliverableReadModel:
    def __init__(
        self,
        conn: sqlite3.Connection,
        rows: TeamMissionRowMapper,
    ) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._rows = rows

    def project(
        self,
        conversation: dict[str, Any],
        missions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        mission_ids = [
            _text(mission.get("mission_id"))
            for mission in missions
            if _text(mission.get("mission_id"))
        ]
        mission_id_set = set(mission_ids)
        conversation_session_id = _text(
            conversation.get("conversation_session_id")
            or conversation.get("conversationSessionId")
        )

        last_message: dict[str, Any] = {}
        final_deliverables: list[dict[str, Any]] = []
        if conversation_session_id:
            with self._lock:
                last_message_row = self._conn.execute(
                    """
                    SELECT *
                    FROM messages
                    WHERE session_id = ?
                      AND active = 1
                      AND role IN ('user', 'assistant')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (conversation_session_id,),
                ).fetchone()
                final_rows = self._conn.execute(
                    """
                    SELECT *
                    FROM messages
                    WHERE session_id = ?
                      AND active = 1
                      AND role = 'assistant'
                      AND metadata_json LIKE ?
                    ORDER BY id ASC
                    """,
                    (conversation_session_id, "%final_deliverable%"),
                ).fetchall()
            last_message = message_summary_from_message(
                self._rows.message_from_row(last_message_row)
            )
            for row in final_rows:
                deliverable = final_deliverable_from_message(
                    self._rows.message_from_row(row)
                )
                if deliverable and _text(deliverable.get("mission_id")) in mission_id_set:
                    final_deliverables.append(deliverable)

        if not mission_ids:
            return _projection(
                last_message=last_message,
                final_deliverables=[],
                artifact_refs_by_mission={},
                artifact_refs_by_task={},
                all_artifact_refs=[],
            )

        placeholders = ",".join("?" for _ in mission_ids)
        memory_params: list[Any] = [*mission_ids, MEMORY_COMMITTED_STATUS]
        memory_clauses = [f"mission_id IN ({placeholders})", "status = ?"]
        if conversation_session_id:
            memory_clauses.append("conversation_session_id = ?")
            memory_params.append(conversation_session_id)
        with self._lock:
            memory_rows = self._conn.execute(
                f"""
                SELECT *
                FROM team_mission_memory_items
                WHERE {' AND '.join(memory_clauses)}
                ORDER BY created_at ASC, updated_at ASC, id ASC
                """,
                tuple(memory_params),
            ).fetchall()

        artifact_refs_by_mission: dict[str, list[dict[str, Any]]] = {}
        artifact_refs_by_task: dict[tuple[str, str], list[dict[str, Any]]] = {}
        all_artifact_refs: list[dict[str, Any]] = []
        for row in memory_rows:
            item = self._rows.memory_item_from_row(row)
            if not item:
                continue
            refs = dedupe_artifact_refs(list(item.get("artifact_refs") or []))
            if not refs:
                continue
            mission_id = _text(item.get("mission_id"))
            task_id = _text(item.get("task_id"))
            artifact_refs_by_mission[mission_id] = dedupe_artifact_refs([
                *artifact_refs_by_mission.get(mission_id, []),
                *refs,
            ])
            if task_id:
                artifact_refs_by_task[(mission_id, task_id)] = dedupe_artifact_refs([
                    *artifact_refs_by_task.get((mission_id, task_id), []),
                    *refs,
                ])
            all_artifact_refs.extend(refs)

        final_deliverables = [
            final_deliverable_with_artifact_refs(
                deliverable,
                artifact_refs_by_mission,
                artifact_refs_by_task,
            )
            for deliverable in final_deliverables
        ]
        return _projection(
            last_message=last_message,
            final_deliverables=final_deliverables,
            artifact_refs_by_mission=artifact_refs_by_mission,
            artifact_refs_by_task=artifact_refs_by_task,
            all_artifact_refs=all_artifact_refs,
        )


def _projection(
    *,
    last_message: dict[str, Any],
    final_deliverables: list[dict[str, Any]],
    artifact_refs_by_mission: dict[str, list[dict[str, Any]]],
    artifact_refs_by_task: dict[tuple[str, str], list[dict[str, Any]]],
    all_artifact_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    final_deliverables_by_mission: dict[str, list[dict[str, Any]]] = {}
    final_deliverables_by_task: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for deliverable in final_deliverables:
        mission_id = _text(deliverable.get("mission_id"))
        task_id = _text(deliverable.get("task_id"))
        final_deliverables_by_mission.setdefault(mission_id, []).append(deliverable)
        if task_id:
            final_deliverables_by_task.setdefault((mission_id, task_id), []).append(
                deliverable
            )
    last_message = message_with_deliverable_artifact_refs(
        last_message,
        final_deliverables,
    )
    return {
        "last_message": last_message,
        "last_message_preview": _text(last_message.get("preview")),
        "last_message_at": last_message.get("timestamp") or 0,
        "final_deliverables": final_deliverables,
        "final_deliverables_by_mission": final_deliverables_by_mission,
        "final_deliverables_by_task": final_deliverables_by_task,
        "artifact_refs": dedupe_artifact_refs(all_artifact_refs),
        "artifact_refs_by_mission": artifact_refs_by_mission,
        "artifact_refs_by_task": artifact_refs_by_task,
    }


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["ConversationDeliverableReadModel"]
