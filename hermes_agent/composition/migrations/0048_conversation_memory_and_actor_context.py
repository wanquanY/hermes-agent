"""Add participant actor state, conversation memory, and context summaries."""

from __future__ import annotations

import sqlite3
import json

from hermes_agent.storage.conversation_memory_schema import (
    CONVERSATION_MEMORY_SCHEMA_SQL,
)


version = 48
description = "participant actor state and canonical conversation memory"

_TEAM_PROMPT_PREFIXES = (
    "You are the Team Leader for a Dovie team conversation.",
    "You are the Team Leader in a Dovie team conversation.",
    "You are an addressed member in a DoXie team conversation.",
)


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _json_list(value: object) -> list[object]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _clean_polluted_user_messages(cursor: sqlite3.Cursor) -> None:
    """Repair old team prompts that were persisted as user-authored speech.

    Recover the real user suffix when the legacy renderer included it. Rows
    containing only an internal control prompt are soft-deactivated; canonical
    team submission rows, when present, remain untouched.
    """
    rows = cursor.execute(
        "SELECT id, content, metadata_json FROM messages WHERE role = 'user' AND active = 1"
    ).fetchall()
    for row in rows:
        content = row[1]
        if not isinstance(content, str) or not content.startswith(_TEAM_PROMPT_PREFIXES):
            continue
        marker = "\n\nUser message:\n"
        metadata_raw = row[2] or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["role_contract_migration"] = {
            "version": 48,
            "reason": "legacy team runtime context was persisted as user content",
        }
        if marker in content and content.rsplit(marker, 1)[1].strip():
            cursor.execute(
                "UPDATE messages SET content = ?, metadata_json = ? WHERE id = ?",
                (
                    content.rsplit(marker, 1)[1].strip(),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    int(row[0]),
                ),
            )
        else:
            cursor.execute(
                "UPDATE messages SET active = 0, metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), int(row[0])),
            )


def _backfill_legacy_team_memory(cursor: sqlite3.Cursor) -> None:
    if not _table_exists(cursor, "team_mission_memory_items"):
        return
    allowed_kinds = {
        "fact", "preference", "decision", "commitment", "constraint",
        "risk", "open_question", "artifact", "summary",
    }
    rows = cursor.execute(
        """
        SELECT legacy.*
          FROM team_mission_memory_items AS legacy
          JOIN sessions ON sessions.id = legacy.conversation_session_id
        """
    ).fetchall()
    columns = [item[0] for item in cursor.description or []]
    for raw in rows:
        item = dict(zip(columns, raw))
        legacy_id = str(item.get("id") or "").strip()
        conversation = str(item.get("conversation_session_id") or "").strip()
        mission_id = str(item.get("mission_id") or "").strip()
        scope = str(item.get("scope") or "").strip()
        if not legacy_id or not conversation:
            continue
        activity_id = f"mission:{mission_id}" if mission_id else ""
        owner_kind = "activity" if scope in {"mission", "mission_task", "task", "node"} else "conversation"
        owner_id = activity_id if owner_kind == "activity" else conversation
        kind = str(item.get("kind") or "fact").strip()
        if kind not in allowed_kinds:
            kind = "fact"
        status = str(item.get("status") or "proposed").strip()
        if status not in {"proposed", "committed", "superseded", "invalidated"}:
            status = "proposed"
        visibility = (
            {"kind": "activity", "activity_id": activity_id}
            if owner_kind == "activity"
            else {"kind": "conversation"}
        )
        provenance = {
            "legacy_memory_id": legacy_id,
            "source_node_ids": _json_list(item.get("source_node_ids_json")),
            "source_run_ids": _json_list(item.get("source_run_ids_json")),
        }
        cursor.execute(
            """
            INSERT OR IGNORE INTO conversation_memory_items (
                memory_id, conversation_session_id, owner_kind, owner_id,
                participant_id, activity_id, node_id, kind, content,
                structured_payload_json, visibility_json, provenance_json,
                supersedes_json, confidence, status, revision, valid_from,
                valid_until, invalidated_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '', ?, '', ?, ?, ?, ?, ?, '[]', ?, ?, 1, ?, NULL, ?, ?, ?)
            """,
            (
                f"conversation-memory:{legacy_id}",
                conversation,
                owner_kind,
                owner_id,
                activity_id,
                kind,
                str(item.get("content") or ""),
                item.get("structured_payload_json") or "{}",
                json.dumps(visibility, ensure_ascii=False, sort_keys=True),
                json.dumps(provenance, ensure_ascii=False, sort_keys=True),
                max(0.0, min(1.0, float(item.get("confidence") or 0))),
                status,
                float(item.get("created_at") or 0),
                float(item.get("invalidated_at")) if item.get("invalidated_at") is not None else None,
                float(item.get("created_at") or 0),
                float(item.get("updated_at") or item.get("created_at") or 0),
            ),
        )


def _columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    return {str(row[1]) for row in cursor.execute(f'PRAGMA table_info("{table}")')}


def _add_column(cursor: sqlite3.Cursor, table: str, name: str, declaration: str) -> None:
    if name in _columns(cursor, table):
        return
    cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')


def apply(cursor: sqlite3.Cursor) -> None:
    _add_column(
        cursor,
        "conversation_participants",
        "memory_namespace",
        "TEXT NOT NULL DEFAULT ''",
    )
    _add_column(
        cursor,
        "conversation_participants",
        "transcript_cursor",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        cursor,
        "conversation_participants",
        "memory_revision",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        cursor,
        "conversation_participants",
        "status",
        "TEXT NOT NULL DEFAULT 'active'",
    )
    cursor.executescript(CONVERSATION_MEMORY_SCHEMA_SQL)
    cursor.execute(
        """
        UPDATE conversation_participants
           SET memory_namespace = 'conversation:' || conversation_session_id
               || '/participant:' || participant_id
         WHERE memory_namespace = ''
        """
    )
    _backfill_legacy_team_memory(cursor)
    _clean_polluted_user_messages(cursor)


__all__ = ["apply", "description", "version"]
