from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict

from hermes_team_mission_memory_utils import text as _text


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def rename_team_mission_conversation(db: Any, identifier: str, title: str) -> Dict[str, Any]:
    identifier = _text(identifier)
    title = _text(title)
    if not identifier or not title:
        return {}
    conversation = db.resolve_team_mission_conversation(identifier).get("conversation") or {}
    conversation_id = _text(conversation.get("conversation_id"))
    if not conversation_id:
        return {}
    stable_session_id = _text(conversation.get("stable_session_id"))
    cleaned_title = db.sanitize_title(title)
    if not cleaned_title:
        return {}
    if stable_session_id and not db.get_session(stable_session_id):
        db.create_session(stable_session_id, source="team_mission", transient=False)
    if stable_session_id:
        db.set_session_title(stable_session_id, cleaned_title)

    updated_at = time.time()

    def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
        conn.execute(
            """
            UPDATE team_mission_conversations
            SET title = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            (cleaned_title, updated_at, conversation_id),
        )
        return db._team_mission_conversation_from_row(conn.execute(
            "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()) or {}

    renamed = db._execute_write(_do)
    return {
        "conversation": renamed,
        "conversation_id": conversation_id,
        "stable_session_id": stable_session_id,
        "title": cleaned_title,
    }


def delete_team_mission_conversation(db: Any, identifier: str) -> Dict[str, Any]:
    identifier = _text(identifier)
    if not identifier:
        return {}
    resolved = db.resolve_team_mission_conversation(identifier)
    conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
    conversation_id = _text((conversation or {}).get("conversation_id"))
    if not conversation_id:
        return {}
    stable_session_id = _text((conversation or {}).get("stable_session_id"))
    with db._lock:
        mission_ids = [
            _text(row["mission_id"])
            for row in db._conn.execute(
                "SELECT mission_id FROM team_missions WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            if _text(row["mission_id"])
        ]
        if not mission_ids:
            active_mission_id = _text((conversation or {}).get("active_mission_id"))
            if active_mission_id:
                mission_ids = [active_mission_id]
        if mission_ids:
            placeholders = ",".join("?" for _ in mission_ids)
            binding_rows = db._conn.execute(
                f"""
                SELECT DISTINCT session_id, runtime_session_id
                FROM team_mission_run_bindings
                WHERE mission_id IN ({placeholders})
                """,
                tuple(mission_ids),
            ).fetchall()
        else:
            binding_rows = []
    run_session_ids: list[str] = []
    for row in binding_rows:
        for key in ("session_id", "runtime_session_id"):
            value = _text(_row_value(row, key, ""))
            if value and value not in run_session_ids:
                run_session_ids.append(value)

    def _do(conn: sqlite3.Connection) -> bool:
        if mission_ids:
            placeholders = ",".join("?" for _ in mission_ids)
            memory_ids = [
                _text(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM team_mission_memory_items WHERE mission_id IN ({placeholders})",
                    tuple(mission_ids),
                ).fetchall()
                if _text(row["id"])
            ]
            if memory_ids:
                memory_placeholders = ",".join("?" for _ in memory_ids)
                conn.execute(
                    f"""
                    DELETE FROM team_mission_memory_edges
                    WHERE from_memory_id IN ({memory_placeholders})
                       OR to_memory_id IN ({memory_placeholders})
                    """,
                    tuple(memory_ids + memory_ids),
                )
            for table in (
                "team_mission_memory_items",
                "team_mission_artifacts",
                "team_mission_run_bindings",
                "team_mission_edges",
                "team_mission_nodes",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE mission_id IN ({placeholders})",
                    tuple(mission_ids),
                )
        conn.execute("DELETE FROM team_mission_conversations WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM team_missions WHERE conversation_id = ?", (conversation_id,))
        return True

    db._execute_write(_do)
    return {
        "deleted": True,
        "conversation_id": conversation_id,
        "stable_session_id": stable_session_id,
        "mission_ids": mission_ids,
        "run_session_ids": run_session_ids,
    }
