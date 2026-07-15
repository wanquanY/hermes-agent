"""Remove legacy participant identity envelopes from canonical messages."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_agent.domain.participant_message_content import (
    LEGACY_SPEAKER_ENVELOPE_VERSION,
    strip_legacy_speaker_envelopes,
)


version = 51
description = "remove participant speaker envelopes from canonical message content"


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return (
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _metadata(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _participant_id(row: sqlite3.Row, metadata: dict[str, Any]) -> str:
    return str(
        row["participant_id"]
        or metadata.get("participant_id")
        or metadata.get("participantId")
        or metadata.get("speaker_participant_id")
        or ""
    ).strip()


def _known_participant_ids(
    cursor: sqlite3.Cursor,
    session_id: str,
) -> set[str]:
    if not _table_exists(cursor, "conversation_participants"):
        return set()
    return {
        str(row[0] or "").strip()
        for row in cursor.execute(
            "SELECT participant_id FROM conversation_participants "
            "WHERE conversation_session_id = ?",
            (session_id,),
        ).fetchall()
        if str(row[0] or "").strip()
    }


def apply(cursor: sqlite3.Cursor) -> None:
    if not _table_exists(cursor, "messages"):
        return
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(
        "SELECT id, session_id, role, content, participant_id, metadata_json "
        "FROM messages WHERE active = 1 AND role IN ('user', 'assistant') "
        "AND content LIKE '[%'"
    ).fetchall()
    participant_ids_by_session: dict[str, set[str]] = {}
    for row in rows:
        content = row["content"]
        if not isinstance(content, str) or not content.startswith("["):
            continue
        metadata = _metadata(row["metadata_json"])
        session_id = str(row["session_id"] or "").strip()
        known_ids = participant_ids_by_session.setdefault(
            session_id,
            _known_participant_ids(cursor, session_id),
        )
        participant_id = _participant_id(row, metadata)
        legacy_projection = (
            str(metadata.get("speaker_envelope_version") or "").strip()
            == LEGACY_SPEAKER_ENVELOPE_VERSION
        )
        cleaned, removed = strip_legacy_speaker_envelopes(
            content,
            participant_id=participant_id,
            known_participant_ids=known_ids,
            trusted_legacy_projection=legacy_projection,
        )
        if not removed:
            continue

        role = str(row["role"] or "").strip()
        original_role = str(metadata.get("speaker_original_role") or "").strip()
        if legacy_projection and original_role in {"user", "assistant"}:
            role = original_role
        for key in (
            "speaker_envelope_version",
            "speaker_projection_version",
            "speaker_original_role",
            "speaker_projected_role",
            "speaker_provider_name",
        ):
            metadata.pop(key, None)
        metadata["participant_content_migration"] = {
            "version": version,
            "removed_legacy_envelopes": removed,
        }
        cursor.execute(
            "UPDATE messages SET role = ?, content = ?, metadata_json = ? WHERE id = ?",
            (
                role,
                cleaned,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                int(row["id"]),
            ),
        )


__all__ = ["apply", "description", "version"]
