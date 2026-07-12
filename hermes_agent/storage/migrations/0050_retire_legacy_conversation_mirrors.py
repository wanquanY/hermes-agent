"""Backfill legacy deliverables and remove duplicate conversation mirrors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any


version = 50
description = "retire legacy team and member conversation mirror rows"


def _table_exists(cursor: sqlite3.Cursor, name: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return _record(parsed)


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = _loads(row["payload_json"])
    if payload:
        return payload
    return _record(_loads(row["event_json"]).get("payload"))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _source_text(
    cursor: sqlite3.Cursor,
    *,
    source_session_id: str,
    source_run_id: str,
) -> str:
    content = ""
    rows = cursor.execute(
        """
        SELECT payload_json, event_json
          FROM run_events
         WHERE session_id = ? AND run_id = ?
           AND event_type IN ('message.delta', 'message.complete')
         ORDER BY seq ASC
        """,
        (source_session_id, source_run_id),
    ).fetchall()
    for row in rows:
        payload = _payload(row)
        if _text(payload.get("status")).lower() in {"error", "failed"}:
            continue
        snapshot = payload.get("snapshot")
        chunk = snapshot if snapshot is not None else payload.get("delta")
        if chunk is None and _text(payload.get("status")).lower() not in {"error", "failed"}:
            chunk = payload.get("text") or payload.get("content")
        text = _text(chunk)
        if not text:
            continue
        if snapshot is not None or _text(payload.get("mode")).lower() == "snapshot":
            content = text
        elif not content.endswith(text):
            content += text
    return _text(content)


def _backfill_deliverables(cursor: sqlite3.Cursor) -> None:
    if not _table_exists(cursor, "team_mission_deliverables"):
        return
    rows = cursor.execute(
        """
        SELECT id, payload_json, event_json, timestamp
          FROM run_events
         WHERE event_type = 'message.complete'
           AND json_valid(payload_json)
           AND json_valid(event_json)
           AND (
             json_extract(payload_json, '$.team_mission_conversation_mirror') = 1
             OR json_extract(event_json, '$.payload.team_mission_conversation_mirror') = 1
           )
           AND (
             json_extract(payload_json, '$.team_mission_final_deliverable') = 1
             OR json_extract(event_json, '$.payload.team_mission_final_deliverable') = 1
           )
         ORDER BY id ASC
        """
    ).fetchall()
    for row in rows:
        payload = _payload(row)
        mission_id = _text(payload.get("mission_id"))
        node_id = _text(payload.get("node_id"))
        source_run_id = _text(payload.get("source_run_id"))
        source_session_id = _text(payload.get("source_session_id"))
        if not mission_id or not node_id or not source_run_id:
            continue
        if cursor.execute(
            "SELECT 1 FROM team_mission_deliverables WHERE run_id = ? LIMIT 1",
            (source_run_id,),
        ).fetchone():
            continue
        if not cursor.execute(
            "SELECT 1 FROM team_missions WHERE mission_id = ?", (mission_id,)
        ).fetchone():
            continue
        summary = _source_text(
            cursor,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
        ) or _text(payload.get("text") or payload.get("content"))
        if not summary:
            continue
        summary = " ".join(summary.split())[:1600]
        digest = hashlib.sha256(
            f"{mission_id}\x1f{node_id}\x1f{source_run_id}".encode()
        ).hexdigest()[:24]
        now = float(row["timestamp"] or time.time())
        cursor.execute(
            """
            INSERT OR IGNORE INTO team_mission_deliverables (
                deliverable_id, mission_id, node_id, run_id, task_id, status,
                result, summary, payload_json, artifact_refs_json,
                next_context_json, output_contract_json, source, confidence,
                visibility, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'completed', 'PASS', ?, ?, '[]', '{}',
                      '{}', 'legacy_imported', 0.7, 'handoff', ?, ?)
            """,
            (
                f"deliverable-legacy-{digest}",
                mission_id,
                node_id,
                source_run_id,
                _text(payload.get("task_id")) or None,
                summary,
                json.dumps(
                    {
                        "legacy_import_source": "conversation_mirror",
                        "source_session_id": source_session_id,
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )


def apply(cursor: sqlite3.Cursor) -> None:
    if not _table_exists(cursor, "run_events"):
        return
    _backfill_deliverables(cursor)
    legacy_ids = [
        int(row[0])
        for row in cursor.execute(
            """
            SELECT id FROM run_events
             WHERE (json_valid(payload_json) AND (
                       json_extract(payload_json, '$.team_mission_conversation_mirror') = 1
                       OR json_extract(payload_json, '$.member_chat_conversation_mirror') = 1
                   ))
                OR (json_valid(event_json) AND (
                       json_extract(event_json, '$.payload.team_mission_conversation_mirror') = 1
                       OR json_extract(event_json, '$.payload.member_chat_conversation_mirror') = 1
                   ))
            """
        ).fetchall()
    ]
    if legacy_ids:
        placeholders = ",".join("?" for _ in legacy_ids)
        if _table_exists(cursor, "run_event_search_index"):
            cursor.execute(
                f"DELETE FROM run_event_search_index WHERE run_event_id IN ({placeholders})",
                legacy_ids,
            )
        cursor.execute(f"DELETE FROM run_events WHERE id IN ({placeholders})", legacy_ids)
    cursor.execute(
        "DELETE FROM runs WHERE run_id LIKE 'team-mission:%:conversation:%'"
    )
    if _table_exists(cursor, "messages"):
        cursor.execute(
            """
            DELETE FROM messages
             WHERE session_id LIKE 'memberchat:%'
                OR (json_valid(metadata_json) AND (
                    json_extract(metadata_json, '$.member_chat_conversation_mirror') = 1
                    OR json_extract(metadata_json, '$.team_mission_conversation_mirror') = 1
                ))
            """
        )


__all__ = ["apply", "description", "version"]
