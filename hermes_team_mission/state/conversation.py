from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict

from hermes_agent.domain.event_ledger import EventLedger
from hermes_agent.repositories.message_content_codec import decode_message_content
from hermes_team_mission.domain.utils import text as _text

_PLACEHOLDER_TEAM_CONVERSATION_TITLES = {"", "Team Mission", "团队会话"}
_ACTIVE_RUN_STATUS_SQL = "'cancelling','finalizing','queued','running','starting','waiting_approval'"
_EMPTY_TEAM_CONVERSATION_PRUNE_GRACE_SECONDS = 300.0


def is_placeholder_team_mission_conversation_title(title: Any) -> bool:
    return _text(title) in _PLACEHOLDER_TEAM_CONVERSATION_TITLES


def is_replaceable_team_mission_conversation_title(title: Any, display_title_source: Any = "") -> bool:
    if _text(display_title_source) in {"user", "first_user_message"}:
        return False
    return is_placeholder_team_mission_conversation_title(title)


def team_mission_conversation_history_sql(table_name: str = "team_mission_conversations") -> str:
    table_name = _text(table_name) or "team_mission_conversations"
    return (
        f"(EXISTS (SELECT 1 FROM conversation_missions cm WHERE cm.conversation_id = {table_name}.conversation_id AND cm.status = 'active' LIMIT 1) "
        f"OR EXISTS (SELECT 1 FROM team_missions tm WHERE tm.conversation_id = {table_name}.conversation_id LIMIT 1) "
        f"OR EXISTS (SELECT 1 FROM sessions hist_s WHERE hist_s.id = {table_name}.conversation_session_id AND COALESCE(hist_s.message_count, 0) > 0 LIMIT 1) "
        f"OR EXISTS (SELECT 1 FROM runs hist_r WHERE hist_r.session_id = {table_name}.conversation_session_id AND hist_r.status IN ({_ACTIVE_RUN_STATUS_SQL}) LIMIT 1))"
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
        metadata.get("dovie_product_context")
        or metadata.get("dovieProductContext")
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


def _delete_session_rows(conn: sqlite3.Connection, session_ids: list[str]) -> list[str]:
    ordered_ids = list(dict.fromkeys(_text(item) for item in session_ids if _text(item)))
    if not ordered_ids:
        return []
    placeholders = ",".join("?" for _ in ordered_ids)
    # session_index 是写时维护的去规范化索引(侧栏列表的单一数据源)。它的行可能
    # 比 sessions 行活得久 —— 团队会话删除曾清了正式表却漏清索引,导致侧栏一直显示
    # 一个删不掉的「幽灵会话」,点开还报 "did not return canonical conversation id"。
    # 所以无条件按 session_id 清索引,即使 sessions 表里已经没有对应行。
    conn.execute(
        f"DELETE FROM session_index WHERE session_id IN ({placeholders})",
        tuple(ordered_ids),
    )
    existing_ids = {
        _text(row["id"])
        for row in conn.execute(
            f"SELECT id FROM sessions WHERE id IN ({placeholders})",
            tuple(ordered_ids),
        ).fetchall()
        if _text(row["id"])
    }
    if not existing_ids:
        return []
    conn.execute(
        f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({placeholders})",
        tuple(ordered_ids),
    )
    conn.execute(
        f"UPDATE session_lineage SET parent_session_id = NULL WHERE parent_session_id IN ({placeholders})",
        tuple(ordered_ids),
    )
    conn.execute(
        f"""
        DELETE FROM session_branch_requests
        WHERE source_session_id IN ({placeholders})
           OR result_session_id IN ({placeholders})
        """,
        tuple(ordered_ids + ordered_ids),
    )
    conn.execute(
        f"DELETE FROM session_lineage WHERE session_id IN ({placeholders})",
        tuple(ordered_ids),
    )
    EventLedger(conn).delete_sessions(ordered_ids)
    conn.execute(
        f"DELETE FROM run_event_archives WHERE session_id IN ({placeholders})",
        tuple(ordered_ids),
    )
    conn.execute(
        f"""
        DELETE FROM runs
        WHERE session_id IN ({placeholders})
           OR execution_session_id IN ({placeholders})
        """,
        tuple(ordered_ids + ordered_ids),
    )
    conn.execute("DELETE FROM messages WHERE session_id IN ({})".format(placeholders), tuple(ordered_ids))
    conn.execute("DELETE FROM sessions WHERE id IN ({})".format(placeholders), tuple(ordered_ids))
    return [session_id for session_id in ordered_ids if session_id in existing_ids]


def _message_title(db: Any, content: Any) -> str:
    decoded = decode_message_content(content)
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


def _message_page_info(page_info: Any) -> Dict[str, Any]:
    page_info = _json_mapping(page_info)
    prev_cursor = _text(
        page_info.get("prevCursor")
        or page_info.get("prev_cursor")
        or page_info.get("prev_cursor_id")
    )
    next_cursor = _text(
        page_info.get("nextCursor")
        or page_info.get("next_cursor")
        or page_info.get("next_cursor_id")
    )
    try:
        total_count = int(page_info.get("totalCount") or page_info.get("total_count") or 0)
    except (TypeError, ValueError):
        total_count = 0
    return {
        "prevCursor": prev_cursor,
        "nextCursor": next_cursor,
        "prev_cursor_id": prev_cursor or None,
        "next_cursor_id": next_cursor or None,
        "hasMoreBefore": bool(page_info.get("hasMoreBefore") or page_info.get("has_more_before")),
        "hasMoreAfter": bool(page_info.get("hasMoreAfter") or page_info.get("has_more_after")),
        "totalCount": total_count,
    }


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("content"))
    if isinstance(value, list):
        return " ".join(
            part for part in (
                _message_text(item) for item in value
            ) if part
        )
    return _text(value)


def _projection_message(message: Any) -> Dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    projected = dict(message)
    if not _text(projected.get("text")):
        projected["text"] = _message_text(projected.get("content"))
    return projected


def team_mission_conversation_message_page(
    db: Any,
    conversation: Dict[str, Any] | None,
    *,
    limit: int = 100,
) -> Dict[str, Any]:
    conversation = conversation if isinstance(conversation, dict) else {}
    conversation_session_id = _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
    )
    if not conversation_session_id:
        return {
            "messages": [],
            "pageInfo": _message_page_info({}),
        }
    try:
        page = db.get_messages_page_as_conversation(
            conversation_session_id,
            direction="tail",
            limit=limit,
            include_ancestors=False,
            include_inactive=False,
        )
    except Exception:
        return {
            "messages": [],
            "pageInfo": _message_page_info({}),
        }
    messages = [
        message for message in (
            _projection_message(item)
            for item in (page.get("messages") if isinstance(page, dict) else []) or []
        ) if message
    ]
    return {
        "messages": messages,
        "pageInfo": _message_page_info(page.get("pageInfo") if isinstance(page, dict) else {}),
    }


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
    conversation_session_id = session_id
    conversation_id = _text(
        team_context.get("conversation_id")
        or team_context.get("conversationId")
        or conversation_session_id
    )
    if not conversation_id:
        return {}
    existing_session = db.get_session(conversation_session_id) or {}
    conversation = db.ensure_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        mission_id=_text(team_context.get("mission_id") or team_context.get("missionId")),
        team_id=_text(team_context.get("team_id") or team_context.get("teamId")),
        title=_text(title or existing_session.get("title") or team_context.get("title")),
        workspace_id=_text(team_context.get("workspace_id") or team_context.get("workspaceId")),
        workspace_path=_text(team_context.get("workspace_path") or team_context.get("workspacePath")),
        metadata={
            **team_context,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "conversationTeamSessionId": conversation_session_id,
        },
    )

    updater = getattr(db, "update_session_source", None)
    if callable(updater):
        updater(conversation_session_id, "team_mission")
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
                   c.conversation_session_id AS conversation_session_id,
                   c.metadata_json AS metadata_json,
                   m.content AS content
            FROM team_mission_conversations c
            INNER JOIN messages m ON m.session_id = c.conversation_session_id
            WHERE COALESCE(c.title, '') IN ('', 'Team Mission', '团队会话')
              AND m.active = 1
              AND m.role = 'user'
              AND m.id = (
                  SELECT first_m.id
                  FROM messages first_m
                  WHERE first_m.session_id = c.conversation_session_id
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
    updates: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for row in rows:
        conversation_id = _text(_row_value(row, "conversation_id", ""))
        if not conversation_id or conversation_id in seen:
            continue
        metadata = _json_mapping(_row_value(row, "metadata_json", ""))
        if _text(metadata.get("display_title_source") or metadata.get("displayTitleSource")) == "user":
            continue
        title = _message_title(db, _row_value(row, "content", ""))
        if not title:
            continue
        metadata["display_title_source"] = "first_user_message"
        seen.add(conversation_id)
        updates.append((
            title,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            conversation_id,
            _text(_row_value(row, "conversation_session_id", "")),
        ))
    if not updates:
        return 0

    def _do(conn: sqlite3.Connection) -> int:
        repaired = 0
        for title, metadata_json, conversation_id, _conversation_session_id in updates:
            cursor = conn.execute(
                """
                UPDATE team_mission_conversations
                SET title = ?, metadata_json = ?
                WHERE conversation_id = ?
                  AND COALESCE(title, '') IN ('', 'Team Mission', '团队会话')
                """,
                (title, metadata_json, conversation_id),
            )
            repaired += cursor.rowcount
        return repaired

    return db._execute_write(_do) or 0


def prune_empty_team_mission_conversations(
    db: Any,
    *,
    limit: int = 5000,
    min_age_seconds: float = _EMPTY_TEAM_CONVERSATION_PRUNE_GRACE_SECONDS,
) -> int:
    bounded_limit = max(1, min(int(limit or 5000), 10000))
    cutoff = time.time() - max(0.0, float(min_age_seconds or 0.0))
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT c.conversation_id AS conversation_id,
                   c.conversation_session_id AS conversation_session_id
            FROM team_mission_conversations c
            WHERE COALESCE(c.updated_at, c.created_at, 0) <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM conversation_missions cm
                  WHERE cm.conversation_id = c.conversation_id
                    AND cm.status = 'active'
                  LIMIT 1
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM team_missions tm
                  WHERE tm.conversation_id = c.conversation_id
                  LIMIT 1
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM messages m
                  WHERE m.session_id = c.conversation_session_id
                    AND m.active = 1
                  LIMIT 1
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM runs r
                  WHERE r.session_id = c.conversation_session_id
                    AND r.status IN ('cancelling','finalizing','queued','running','starting','waiting_approval')
                  LIMIT 1
              )
            ORDER BY c.updated_at DESC, c.conversation_id ASC
            LIMIT ?
            """,
            (cutoff, bounded_limit),
        ).fetchall()
    conversation_ids = [
        _text(_row_value(row, "conversation_id", ""))
        for row in rows
        if _text(_row_value(row, "conversation_id", ""))
    ]
    if not conversation_ids:
        return 0
    conversation_session_ids = [
        _text(_row_value(row, "conversation_session_id", ""))
        for row in rows
        if _text(_row_value(row, "conversation_session_id", ""))
    ]

    def _do(conn: sqlite3.Connection) -> int:
        placeholders = ",".join("?" for _ in conversation_ids)
        conn.execute(
            f"DELETE FROM team_mission_conversations WHERE conversation_id IN ({placeholders})",
            tuple(conversation_ids),
        )
        conn.execute(
            f"DELETE FROM session_index WHERE conversation_id IN ({placeholders})",
            tuple(conversation_ids),
        )
        if conversation_session_ids:
            session_placeholders = ",".join("?" for _ in conversation_session_ids)
            conn.execute(
                f"DELETE FROM session_index WHERE session_id IN ({session_placeholders})",
                tuple(conversation_session_ids),
            )
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
                tuple(conversation_session_ids),
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
    conversation_session_id = _text(conversation.get("conversation_session_id"))
    cleaned_title = db.sanitize_title(title)
    if not cleaned_title:
        return {}
    if conversation_session_id and not db.get_session(conversation_session_id):
        db.create_session(conversation_session_id, source="team_mission", transient=False)

    updated_at = time.time()

    def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT metadata_json FROM team_mission_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        metadata = _json_mapping(_row_value(row, "metadata_json", ""))
        metadata["display_title_source"] = "user"
        conn.execute(
            """
            UPDATE team_mission_conversations
            SET title = ?, metadata_json = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            (cleaned_title, json.dumps(metadata, ensure_ascii=False, sort_keys=True), updated_at, conversation_id),
        )
        return db._team_mission_conversation_from_row(conn.execute(
            "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()) or {}

    renamed = db._execute_write(_do)
    return {
        "conversation": renamed,
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "title": cleaned_title,
    }


def _delete_orphan_team_conversation_index(db: Any, identifier: str) -> Dict[str, Any]:
    """清理「正式数据已删、只剩去规范化索引」的幽灵团队会话。

    当 team_mission_conversations / team_missions / sessions 都已删除,但
    session_index 行还在时,resolve_team_mission_conversation 找不到正式会话,
    删除流程会整体放弃 → 这个会话在侧栏删不掉、还报错。这里按 identifier
    (可能是 conversation_id 或 conversation_session_id)直接清掉残留索引行,以及
    万一还在的 session 残行。
    """
    identifier = _text(identifier)
    if not identifier:
        return {}

    def _do(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT session_id FROM session_index WHERE session_id = ? OR conversation_id = ?",
            (identifier, identifier),
        ).fetchall()
        session_ids = [
            _text(_row_value(row, "session_id", ""))
            for row in rows
            if _text(_row_value(row, "session_id", ""))
        ]
        # _delete_session_rows 会一并清掉这些 session_id 的 session_index 行;
        # 再按 conversation_id 兜底删一次,防止索引行的 session_id 与传入的
        # identifier 不一致而漏删。
        cleaned = _delete_session_rows(conn, session_ids or [identifier])
        conn.execute(
            "DELETE FROM session_index WHERE session_id = ? OR conversation_id = ?",
            (identifier, identifier),
        )
        return cleaned

    cleaned = db._execute_write(_do) or []
    return {
        "deleted": True,
        "conversation_id": identifier if identifier.startswith("team-conversation") else "",
        "conversation_session_id": "",
        "mission_ids": [],
        "run_session_ids": [],
        "deleted_session_ids": cleaned,
        "orphan_index_cleaned": True,
    }


def delete_team_mission_conversation(db: Any, identifier: str) -> Dict[str, Any]:
    identifier = _text(identifier)
    if not identifier:
        return {}
    resolved = db.resolve_team_mission_conversation(identifier)
    conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
    conversation_id = _text((conversation or {}).get("conversation_id"))
    if not conversation_id:
        # 正式会话已不存在,但去规范化索引可能仍残留 → 清掉幽灵,别直接放弃。
        return _delete_orphan_team_conversation_index(db, identifier)
    conversation_session_id = _text((conversation or {}).get("conversation_session_id"))
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
                SELECT DISTINCT session_id, execution_session_id
                FROM team_mission_run_bindings
                WHERE mission_id IN ({placeholders})
                """,
                tuple(mission_ids),
            ).fetchall()
        else:
            binding_rows = []
    run_session_ids: list[str] = []
    for row in binding_rows:
        for key in ("session_id", "execution_session_id"):
            value = _text(_row_value(row, key, ""))
            if value and value not in run_session_ids:
                run_session_ids.append(value)
    session_ids_to_delete = [
        session_id for session_id in [conversation_session_id, *run_session_ids] if session_id
    ]

    def _do(conn: sqlite3.Connection) -> list[str]:
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
        return _delete_session_rows(conn, session_ids_to_delete)

    deleted_session_ids = db._execute_write(_do) or []
    return {
        "deleted": True,
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "mission_ids": mission_ids,
        "run_session_ids": run_session_ids,
        "deleted_session_ids": deleted_session_ids,
    }
