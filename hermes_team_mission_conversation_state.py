from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict

from hermes_team_mission_memory_utils import text as _text

_PLACEHOLDER_TEAM_CONVERSATION_TITLES = {"", "Team Mission", "团队会话"}
_ACTIVE_RUN_STATUS_SQL = "'cancelling','finalizing','queued','running','starting','waiting_approval'"


def team_mission_conversation_history_sql(table_name: str = "team_mission_conversations") -> str:
    table_name = _text(table_name) or "team_mission_conversations"
    return (
        f"(COALESCE({table_name}.active_mission_id, '') != '' "
        f"OR EXISTS (SELECT 1 FROM team_missions tm WHERE tm.conversation_id = {table_name}.conversation_id LIMIT 1) "
        f"OR EXISTS (SELECT 1 FROM messages hist_m WHERE hist_m.session_id = {table_name}.stable_session_id AND hist_m.active = 1 LIMIT 1) "
        f"OR EXISTS (SELECT 1 FROM runs hist_r WHERE hist_r.session_id = {table_name}.stable_session_id AND hist_r.status IN ({_ACTIVE_RUN_STATUS_SQL}) LIMIT 1))"
    )


def is_routeable_team_mission_conversation(db: Any, conversation_id: str) -> bool:
    conversation_id = _text(conversation_id)
    if not conversation_id:
        return False
    with db._lock:
        row = db._conn.execute(
            f"""
            SELECT 1
            FROM team_mission_conversations
            WHERE conversation_id = ?
              AND {team_mission_conversation_history_sql()}
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
    return row is not None


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _json_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _team_mission_context_from_metadata(metadata: Any) -> Dict[str, Any]:
    metadata = _json_mapping(metadata)
    product_context = _json_mapping(
        metadata.get("doxie_product_context")
        or metadata.get("doxieProductContext")
    )
    team_context = _json_mapping(
        product_context.get("team_mission")
        or product_context.get("teamMission")
        or metadata.get("team_mission")
        or metadata.get("teamMission")
    )
    if _text(team_context.get("kind")) != "leader_conversation":
        return {}
    return team_context


def _message_title(db: Any, content: Any) -> str:
    decoder = getattr(db, "_decode_content", None)
    decoded = decoder(content) if callable(decoder) else content
    if isinstance(decoded, list):
        parts: list[str] = []
        for item in decoded:
            if isinstance(item, dict):
                parts.append(_text(item.get("text") or item.get("content")))
            else:
                parts.append(_text(item))
        text = " ".join(part for part in parts if part)
    elif isinstance(decoded, dict):
        text = _text(decoded.get("text") or decoded.get("content"))
    else:
        text = _text(decoded)
    text = " ".join(text.split())
    if not text:
        return ""
    try:
        return db.sanitize_title(text[:100]) or ""
    except Exception:
        return ""


def normalize_team_mission_conversation_session(
    db: Any,
    *,
    session_id: str,
    metadata: Any = None,
    metadata_json: str = "",
    title: str = "",
) -> Dict[str, Any]:
    session_id = _text(session_id)
    if not session_id:
        return {}
    team_context = _team_mission_context_from_metadata(
        metadata if metadata is not None else metadata_json
    )
    if not team_context:
        return {}
    stable_session_id = session_id
    conversation_id = _text(
        team_context.get("conversation_id")
        or team_context.get("conversationId")
        or stable_session_id
    )
    if not conversation_id:
        return {}
    existing_session = db.get_session(stable_session_id) or {}
    conversation = db.ensure_team_mission_conversation(
        conversation_id=conversation_id,
        stable_session_id=stable_session_id,
        mission_id=_text(team_context.get("mission_id") or team_context.get("missionId")),
        team_id=_text(team_context.get("team_id") or team_context.get("teamId")),
        title=_text(title or existing_session.get("title") or team_context.get("title")),
        workspace_id=_text(team_context.get("workspace_id") or team_context.get("workspaceId")),
        workspace_path=_text(team_context.get("workspace_path") or team_context.get("workspacePath")),
        metadata={
            **team_context,
            "conversation_id": conversation_id,
            "conversation_session_id": stable_session_id,
            "stableTeamSessionId": stable_session_id,
        },
    )

    def _do(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "UPDATE sessions SET source = 'team_mission' WHERE id = ? AND source != 'team_mission'",
            (stable_session_id,),
        )
        return cursor.rowcount

    db._execute_write(_do)
    return conversation


def repair_legacy_team_mission_conversation_sessions(db: Any, *, limit: int = 1000) -> int:
    bounded_limit = max(1, min(int(limit or 1000), 5000))
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT s.id AS session_id, s.title AS title, m.metadata_json AS metadata_json
            FROM sessions s
            INNER JOIN messages m ON m.session_id = s.id AND m.active = 1
            WHERE s.source != 'team_mission'
              AND m.metadata_json LIKE '%team_mission%'
              AND m.metadata_json LIKE '%leader_conversation%'
            ORDER BY m.timestamp ASC, m.id ASC
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
    repaired = 0
    seen: set[str] = set()
    for row in rows:
        session_id = _text(_row_value(row, "session_id", ""))
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        conversation = normalize_team_mission_conversation_session(
            db,
            session_id=session_id,
            metadata_json=_text(_row_value(row, "metadata_json", "")),
            title=_text(_row_value(row, "title", "")),
        )
        if conversation:
            repaired += 1
    return repaired


def repair_placeholder_team_mission_conversation_titles(db: Any, *, limit: int = 1000) -> int:
    bounded_limit = max(1, min(int(limit or 1000), 5000))
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT c.conversation_id AS conversation_id,
                   c.stable_session_id AS stable_session_id,
                   m.content AS content
            FROM team_mission_conversations c
            INNER JOIN messages m ON m.session_id = c.stable_session_id
            WHERE COALESCE(c.title, '') IN ('', 'Team Mission', '团队会话')
              AND m.active = 1
              AND m.role = 'user'
              AND m.id = (
                  SELECT first_m.id
                  FROM messages first_m
                  WHERE first_m.session_id = c.stable_session_id
                    AND first_m.active = 1
                    AND first_m.role = 'user'
                  ORDER BY first_m.timestamp ASC, first_m.id ASC
                  LIMIT 1
              )
            ORDER BY m.timestamp ASC, m.id ASC
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
    updates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for row in rows:
        conversation_id = _text(_row_value(row, "conversation_id", ""))
        if not conversation_id or conversation_id in seen:
            continue
        title = _message_title(db, _row_value(row, "content", ""))
        if not title:
            continue
        seen.add(conversation_id)
        updates.append((title, conversation_id, _text(_row_value(row, "stable_session_id", ""))))
    if not updates:
        return 0

    def _do(conn: sqlite3.Connection) -> int:
        repaired = 0
        for title, conversation_id, _stable_session_id in updates:
            cursor = conn.execute(
                """
                UPDATE team_mission_conversations
                SET title = ?
                WHERE conversation_id = ?
                  AND COALESCE(title, '') IN ('', 'Team Mission', '团队会话')
                """,
                (title, conversation_id),
            )
            repaired += cursor.rowcount
        return repaired

    return db._execute_write(_do) or 0


def prune_empty_team_mission_conversations(db: Any, *, limit: int = 5000) -> int:
    bounded_limit = max(1, min(int(limit or 5000), 10000))
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT c.conversation_id AS conversation_id,
                   c.stable_session_id AS stable_session_id
            FROM team_mission_conversations c
            WHERE COALESCE(c.active_mission_id, '') = ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM team_missions tm
                  WHERE tm.conversation_id = c.conversation_id
                  LIMIT 1
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM messages m
                  WHERE m.session_id = c.stable_session_id
                    AND m.active = 1
                  LIMIT 1
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM runs r
                  WHERE r.session_id = c.stable_session_id
                    AND r.status IN ('cancelling','finalizing','queued','running','starting','waiting_approval')
                  LIMIT 1
              )
            ORDER BY c.updated_at DESC, c.conversation_id ASC
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
    conversation_ids = [
        _text(_row_value(row, "conversation_id", ""))
        for row in rows
        if _text(_row_value(row, "conversation_id", ""))
    ]
    if not conversation_ids:
        return 0
    stable_session_ids = [
        _text(_row_value(row, "stable_session_id", ""))
        for row in rows
        if _text(_row_value(row, "stable_session_id", ""))
    ]

    def _do(conn: sqlite3.Connection) -> int:
        placeholders = ",".join("?" for _ in conversation_ids)
        conn.execute(
            f"DELETE FROM team_mission_conversations WHERE conversation_id IN ({placeholders})",
            tuple(conversation_ids),
        )
        if stable_session_ids:
            session_placeholders = ",".join("?" for _ in stable_session_ids)
            conn.execute(
                f"""
                DELETE FROM sessions
                WHERE id IN ({session_placeholders})
                  AND source = 'team_mission'
                  AND COALESCE(message_count, 0) = 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM messages m
                      WHERE m.session_id = sessions.id
                        AND m.active = 1
                      LIMIT 1
                  )
                """,
                tuple(stable_session_ids),
            )
        return len(conversation_ids)

    return db._execute_write(_do) or 0


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
