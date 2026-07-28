"""Backfill stable identities for runtime-owned user submissions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_conversation_message_identity import user_conversation_message_id_for


version = 61
description = "backfill stable conversation identities for user submissions"


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return (
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _mapping(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(mapping.get(key) or "").strip()
        if value:
            return value
    return ""


def apply(cursor: sqlite3.Cursor) -> None:
    if not _table_exists(cursor, "messages"):
        return
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(
        """
        SELECT id, session_id, metadata_json
          FROM messages
         WHERE role = 'user'
           AND COALESCE(conversation_message_id, '') = ''
         ORDER BY id
        """
    ).fetchall()
    for row in rows:
        metadata = _mapping(row["metadata_json"])
        turn_id = _text(metadata, "turn_id", "turnId")
        run_id = _text(metadata, "run_id", "runId")
        client_message_id = _text(
            metadata,
            "client_message_id",
            "clientMessageId",
        )
        if not (turn_id or run_id or client_message_id):
            continue
        conversation_message_id = user_conversation_message_id_for(
            session_id=str(row["session_id"] or ""),
            turn_id=turn_id,
            run_id=run_id,
            client_message_id=client_message_id,
        )
        cursor.execute(
            """
            UPDATE OR IGNORE messages
               SET conversation_message_id = ?
             WHERE id = ?
               AND COALESCE(conversation_message_id, '') = ''
            """,
            (conversation_message_id, int(row["id"])),
        )


__all__ = ["apply", "description", "version"]
