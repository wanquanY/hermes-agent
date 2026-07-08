"""Read model for local session recall/search.

This is the production owner for the ``session_search`` tool's read side. It
keeps recall on explicit SQLite read-model APIs instead of reaching through the
legacy state facade from runtime code.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionRecallUnavailable:
    """Structured unavailable state for callers that need a user-facing error."""

    reason: str


@dataclass(frozen=True)
class _SearchToken:
    value: str
    operator: str


class SessionRecallReadModel:
    """SQLite-backed read model used by the session recall tool."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)

    @classmethod
    def open_default(cls, db_path: Path | str | None = None) -> "SessionRecallReadModel":
        return cls(connect_session_repository_db(db_path))

    @classmethod
    def from_session_db(cls, session_db: Any) -> "SessionRecallReadModel | None":
        conn = getattr(session_db, "_conn", None)
        if isinstance(conn, sqlite3.Connection):
            return cls(conn)
        return None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        stable = str(session_id or "").strip()
        if not stable or not self._table_exists("sessions"):
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (stable,)).fetchone()
        return dict(row) if row else None

    def list_sessions_rich(
        self,
        source: str | None = None,
        exclude_sources: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        page_cursor: dict[str, Any] | None = None,
        id_query: str | None = None,
        min_message_count: int = 0,
        archived: str = "false",
    ) -> list[dict[str, Any]]:
        if not self._table_exists("sessions"):
            return []
        bounded_limit = max(1, min(_to_int(limit, 20), 100))
        bounded_offset = max(0, _to_int(offset, 0))
        min_messages = max(0, _to_int(min_message_count, 0))
        archived_mode = str(archived or "false").strip().lower()
        if archived_mode not in {"false", "true", "only", "all"}:
            raise ValueError(f"unsupported archived filter: {archived!r}")
        where: list[str] = []
        params: list[Any] = []
        columns = self._table_columns("sessions")
        has_archived = "archived" in columns
        if not include_children and "parent_session_id" in columns:
            if self._table_exists("session_lineage"):
                where.append(
                    "(parent_session_id IS NULL OR parent_session_id = ''"
                    " OR EXISTS (SELECT 1 FROM session_lineage l"
                    "            WHERE l.session_id = sessions.id"
                    "              AND l.branch_origin = 'user_message_action')"
                    " OR EXISTS (SELECT 1 FROM sessions p"
                    "            WHERE p.id = sessions.parent_session_id"
                    "              AND p.end_reason = 'branched'"
                    "              AND sessions.started_at >= p.ended_at))"
                )
            else:
                where.append("(parent_session_id IS NULL OR parent_session_id = '')")
        if source:
            where.append("source = ?")
            params.append(str(source))
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where.append(f"source NOT IN ({placeholders})")
            params.extend(str(value) for value in exclude_sources)
        if id_query:
            needle = f"%{_escape_like(str(id_query).strip().lower())}%"
            where.append("LOWER(id) LIKE ? ESCAPE '\\'")
            params.append(needle)
        if min_messages > 0:
            where.append("COALESCE(message_count, 0) >= ?")
            params.append(min_messages)
        if has_archived and archived_mode in {"false", "true", "only"}:
            where.append("COALESCE(archived, 0) = ?")
            params.append(1 if archived_mode in {"true", "only"} else 0)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        last_active_expr = (
            "COALESCE(last_active, updated_at, started_at)"
            if {"last_active", "updated_at"}.issubset(columns)
            else "started_at"
        )
        if order_by_last_active and "parent_session_id" in columns:
            sql = f"""
                WITH RECURSIVE chain(root_id, cur_id) AS (
                    SELECT id, id FROM sessions {where_sql}
                    UNION ALL
                    SELECT c.root_id, child.id
                      FROM chain c
                      JOIN sessions parent ON parent.id = c.cur_id
                      JOIN sessions child ON child.parent_session_id = c.cur_id
                     WHERE parent.end_reason = 'compression'
                       AND child.started_at >= parent.ended_at
                ),
                chain_max AS (
                    SELECT root_id, MAX({last_active_expr.replace('last_active', 'ss.last_active').replace('updated_at', 'ss.updated_at').replace('started_at', 'ss.started_at')}) AS effective_last_active
                      FROM chain c
                      JOIN sessions ss ON ss.id = c.cur_id
                     GROUP BY root_id
                )
                SELECT sessions.*,
                       COALESCE(sessions.preview, '') AS _preview_summary,
                       {last_active_expr} AS _last_active_summary,
                       COALESCE(cm.effective_last_active, {last_active_expr}) AS _effective_last_active
                  FROM sessions
                  LEFT JOIN chain_max cm ON cm.root_id = sessions.id
                  {where_sql}
                 ORDER BY _effective_last_active DESC, sessions.started_at DESC, sessions.id DESC
                 LIMIT ? OFFSET ?
            """
            query_params = params + params + [bounded_limit, bounded_offset]
        else:
            order_expr = (
                f"{last_active_expr} DESC, started_at DESC, id DESC"
                if order_by_last_active
                else "started_at DESC, id DESC"
            )
            sql = f"""
                SELECT *,
                       COALESCE(preview, '') AS _preview_summary,
                       {last_active_expr} AS _last_active_summary
                  FROM sessions
                  {where_sql}
                 ORDER BY {order_expr}
                 LIMIT ? OFFSET ?
            """
            query_params = params + [bounded_limit, bounded_offset]
        with self._lock:
            rows = self._conn.execute(sql, query_params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["preview"] = str(item.pop("_preview_summary", item.get("preview") or "") or "")
            item["last_active"] = item.pop(
                "_last_active_summary",
                item.get("last_active") or item.get("updated_at") or item.get("started_at") or 0,
            )
            effective_last_active = item.pop("_effective_last_active", None)
            if effective_last_active is None:
                effective_last_active = item.get("last_active") or item.get("started_at") or 0
            item["_page_cursor"] = {
                "effective_last_active": effective_last_active,
                "started_at": item.get("started_at") or 0,
                "id": item.get("id") or "",
            }
            item["archived"] = bool(item.get("archived") or 0) if has_archived else False
            if "cwd" not in item:
                item["cwd"] = None
            result.append(item)
        if project_compression_tips and not include_children:
            result = self._project_compression_tips(result, has_archived=has_archived)
        return result

    def get_compression_tip(self, session_id: str) -> str:
        current = str(session_id or "").strip()
        if not current:
            return current
        for _ in range(100):
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT id FROM sessions
                     WHERE parent_session_id = ?
                       AND started_at >= (
                           SELECT ended_at FROM sessions
                            WHERE id = ? AND end_reason = 'compression'
                       )
                     ORDER BY started_at DESC LIMIT 1
                    """,
                    (current, current),
                ).fetchone()
            if row is None:
                return current
            current = str(row["id"] or "")
        return current

    def resolve_resume_session_id(self, session_id: str | None) -> str | None:
        """Resolve a user-facing session id to the transcript branch with messages."""

        if session_id is None:
            return None
        stable = str(session_id or "").strip()
        if not stable:
            return stable
        target = self.get_compression_tip(stable)
        if self._session_has_messages(target):
            return target
        current = target
        seen = {current}
        for _ in range(32):
            child_id = self._latest_child_session_id(current)
            if not child_id or child_id in seen:
                return target
            seen.add(child_id)
            if self._session_has_messages(child_id):
                return child_id
            current = child_id
        return target

    def session_count(
        self,
        source: str | None = None,
        *,
        min_message_count: int = 0,
        archived: str = "false",
    ) -> int:
        if not self._table_exists("sessions"):
            return 0
        columns = self._table_columns("sessions")
        archived_mode = str(archived or "false").strip().lower()
        if archived_mode not in {"false", "true", "only", "all"}:
            raise ValueError(f"unsupported archived filter: {archived!r}")
        where: list[str] = []
        params: list[Any] = []
        if source:
            where.append("source = ?")
            params.append(str(source))
        min_messages = max(0, _to_int(min_message_count, 0))
        if min_messages > 0:
            where.append("COALESCE(message_count, 0) >= ?")
            params.append(min_messages)
        if "archived" in columns and archived_mode in {"false", "true", "only"}:
            where.append("COALESCE(archived, 0) = ?")
            params.append(1 if archived_mode in {"true", "only"} else 0)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) AS count FROM sessions {where_sql}", params).fetchone()
        return int(row["count"] or 0) if row else 0

    def search_messages(
        self,
        query: str,
        source_filter: list[str] | None = None,
        exclude_sources: list[str] | None = None,
        role_filter: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        sort: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        if not str(query or "").strip() or not self._table_exists("messages"):
            return []
        bounded_limit = max(1, min(_to_int(limit, 20), 100))
        bounded_offset = max(0, _to_int(offset, 0))
        role_filter = [str(role) for role in (role_filter or []) if str(role or "").strip()]
        sort_norm = str(sort or "").strip().lower()
        order_by = "m.timestamp DESC, m.id DESC" if sort_norm == "newest" else "m.timestamp ASC, m.id ASC" if sort_norm == "oldest" else "m.timestamp DESC, m.id DESC"
        query_text = str(query or "").strip()
        fts_query = _sanitize_fts5_query(query_text)
        if _contains_cjk(query_text):
            return self._search_messages_cjk_like(
                query_text,
                source_filter=source_filter,
                exclude_sources=exclude_sources,
                role_filter=role_filter,
                limit=bounded_limit,
                offset=bounded_offset,
                order_by=order_by,
                include_inactive=include_inactive,
            )
        if self._table_exists("messages_fts") and fts_query:
            try:
                fts_results = self._search_messages_fts(
                    fts_query,
                    source_filter=source_filter,
                    exclude_sources=exclude_sources,
                    role_filter=role_filter,
                    limit=bounded_limit,
                    offset=bounded_offset,
                    order_by=order_by,
                    include_inactive=include_inactive,
                )
                return fts_results
            except sqlite3.OperationalError:
                logger.debug("session recall FTS search failed; falling back to LIKE", exc_info=True)
        return self._search_messages_like(
            query_text,
            source_filter=source_filter,
            exclude_sources=exclude_sources,
            role_filter=role_filter,
            limit=bounded_limit,
            offset=bounded_offset,
            order_by=order_by,
            include_inactive=include_inactive,
        )

    def _project_compression_tips(
        self,
        sessions: list[dict[str, Any]],
        *,
        has_archived: bool,
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for session in sessions:
            if session.get("end_reason") != "compression":
                projected.append(session)
                continue
            tip_id = self.get_compression_tip(str(session.get("id") or ""))
            if not tip_id or tip_id == session.get("id"):
                projected.append(session)
                continue
            tip = self.get_session(tip_id)
            if not tip:
                projected.append(session)
                continue
            merged = dict(session)
            for key in (
                "id",
                "ended_at",
                "end_reason",
                "message_count",
                "tool_call_count",
                "title",
                "display_title",
                "display_title_source",
                "last_active",
                "preview",
                "model",
                "system_prompt",
                "cwd",
            ):
                if key in tip:
                    merged[key] = tip[key]
            merged["_lineage_root_id"] = session.get("id")
            merged["archived"] = bool(merged.get("archived") or 0) if has_archived else False
            merged.setdefault("cwd", None)
            projected.append(merged)
        return projected

    def get_messages_around(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        stable = str(session_id or "").strip()
        anchor_id = _to_int(around_message_id, 0)
        if not stable or anchor_id <= 0 or not self._table_exists("messages"):
            return {"window": [], "messages_before": 0, "messages_after": 0}
        bounded_window = max(0, min(_to_int(window, 5), 20))
        active_clause = "" if include_inactive or "active" not in self._table_columns("messages") else " AND active = 1"
        with self._lock:
            anchor = self._conn.execute(
                f"SELECT 1 FROM messages WHERE id = ? AND session_id = ?{active_clause} LIMIT 1",
                (anchor_id, stable),
            ).fetchone()
            if anchor is None:
                return {"window": [], "messages_before": 0, "messages_after": 0}
            before_rows = self._conn.execute(
                f"SELECT * FROM messages WHERE session_id = ? AND id <= ?{active_clause} "
                "ORDER BY id DESC LIMIT ?",
                (stable, anchor_id, bounded_window + 1),
            ).fetchall()
            after_rows = self._conn.execute(
                f"SELECT * FROM messages WHERE session_id = ? AND id > ?{active_clause} "
                "ORDER BY id ASC LIMIT ?",
                (stable, anchor_id, bounded_window),
            ).fetchall()
        rows = list(reversed(before_rows)) + list(after_rows)
        return {
            "window": [_message_row(row) for row in rows],
            "messages_before": max(0, len(before_rows) - 1),
            "messages_after": len(after_rows),
        }

    def get_anchored_view(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        bookend: int = 3,
        keep_roles: tuple[str, ...] | None = ("user", "assistant"),
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        primitive = self.get_messages_around(
            session_id,
            around_message_id,
            window=window,
            include_inactive=include_inactive,
        )
        window_rows = primitive["window"]
        if not window_rows:
            return {
                "window": [],
                "messages_before": 0,
                "messages_after": 0,
                "bookend_start": [],
                "bookend_end": [],
            }
        anchor_id = _to_int(around_message_id, 0)
        if keep_roles is not None:
            keep = set(keep_roles)
            window_rows = [
                row for row in window_rows
                if row.get("id") == anchor_id or str(row.get("role") or "") in keep
            ]
        first_id = _to_int(window_rows[0].get("id") if window_rows else anchor_id, anchor_id)
        last_id = _to_int(window_rows[-1].get("id") if window_rows else anchor_id, anchor_id)
        bookend = max(0, min(_to_int(bookend, 3), 20))
        return {
            **primitive,
            "window": window_rows,
            "bookend_start": self._bookend(session_id, before_id=first_id, limit=bookend, roles=keep_roles, include_inactive=include_inactive),
            "bookend_end": self._bookend(session_id, after_id=last_id, limit=bookend, roles=keep_roles, include_inactive=include_inactive),
        }

    def _search_messages_fts(
        self,
        query: str,
        *,
        source_filter: list[str] | None,
        exclude_sources: list[str] | None,
        role_filter: list[str],
        limit: int,
        offset: int,
        order_by: str,
        include_inactive: bool,
    ) -> list[dict[str, Any]]:
        where = ["messages_fts MATCH ?"]
        params: list[Any] = [query]
        self._append_message_filters(where, params, source_filter, exclude_sources, role_filter, include_inactive)
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
              FROM messages_fts
              JOIN messages m ON m.id = messages_fts.rowid
              JOIN sessions s ON s.id = m.session_id
             WHERE {' AND '.join(where)}
             ORDER BY {order_by}
             LIMIT ? OFFSET ?
        """
        with self._lock:
            rows = self._conn.execute(sql, params + [limit, offset]).fetchall()
        return [self._search_row(row) for row in rows]

    def _search_messages_like(
        self,
        query: str,
        *,
        source_filter: list[str] | None,
        exclude_sources: list[str] | None,
        role_filter: list[str],
        limit: int,
        offset: int,
        order_by: str,
        include_inactive: bool,
    ) -> list[dict[str, Any]]:
        escaped = _escape_like(query)
        where = ["(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"]
        params: list[Any] = [f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"]
        self._append_message_filters(where, params, source_filter, exclude_sources, role_filter, include_inactive)
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   substr(m.content, max(1, instr(m.content, ?) - 40), 120) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
              FROM messages m
              JOIN sessions s ON s.id = m.session_id
             WHERE {' AND '.join(where)}
             ORDER BY {order_by}
             LIMIT ? OFFSET ?
        """
        with self._lock:
            rows = self._conn.execute(sql, [query] + params + [limit, offset]).fetchall()
        return [self._search_row(row) for row in rows]

    def _search_messages_cjk_like(
        self,
        query: str,
        *,
        source_filter: list[str] | None,
        exclude_sources: list[str] | None,
        role_filter: list[str],
        limit: int,
        offset: int,
        order_by: str,
        include_inactive: bool,
    ) -> list[dict[str, Any]]:
        tokens = _cjk_like_tokens(query)
        if not tokens:
            return []
        where = [_cjk_like_predicate(tokens)]
        params: list[Any] = []
        for token in tokens:
            if token.operator == "NOT":
                continue
            escaped = _escape_like(token.value)
            params.extend([f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"])
        for token in tokens:
            if token.operator != "NOT":
                continue
            escaped = _escape_like(token.value)
            params.extend([f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"])
        self._append_message_filters(where, params, source_filter, exclude_sources, role_filter, include_inactive)
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   substr(m.content, max(1, instr(m.content, ?) - 40), 120) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
              FROM messages m
              JOIN sessions s ON s.id = m.session_id
             WHERE {' AND '.join(where)}
             ORDER BY {order_by}
             LIMIT ? OFFSET ?
        """
        snippet_token = next((token.value for token in tokens if token.operator != "NOT"), tokens[0].value)
        with self._lock:
            rows = self._conn.execute(sql, [snippet_token] + params + [limit, offset]).fetchall()
        return [self._search_row(row) for row in rows]

    def _append_message_filters(
        self,
        where: list[str],
        params: list[Any],
        source_filter: list[str] | None,
        exclude_sources: list[str] | None,
        role_filter: list[str],
        include_inactive: bool,
    ) -> None:
        if not include_inactive and "active" in self._table_columns("messages"):
            where.append("m.active = 1")
        if source_filter:
            placeholders = ",".join("?" for _ in source_filter)
            where.append(f"s.source IN ({placeholders})")
            params.extend(str(value) for value in source_filter)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where.append(f"s.source NOT IN ({placeholders})")
            params.extend(str(value) for value in exclude_sources)
        if role_filter:
            placeholders = ",".join("?" for _ in role_filter)
            where.append(f"m.role IN ({placeholders})")
            params.extend(str(value) for value in role_filter)

    def _search_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("content", None)
        item["context"] = self._message_context(_to_int(item.get("id"), 0))
        return item

    def _message_context(self, message_id: int) -> list[dict[str, str]]:
        if message_id <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                WITH target AS (
                    SELECT session_id, timestamp, id FROM messages WHERE id = ?
                )
                SELECT role, content FROM (
                    SELECT m.id, m.timestamp, m.role, m.content
                      FROM messages m JOIN target t ON t.session_id = m.session_id
                     WHERE (m.timestamp < t.timestamp OR (m.timestamp = t.timestamp AND m.id < t.id))
                       AND COALESCE(m.active, 1) = 1
                     ORDER BY m.timestamp DESC, m.id DESC LIMIT 1
                )
                UNION ALL
                SELECT role, content FROM messages WHERE id = ?
                UNION ALL
                SELECT role, content FROM (
                    SELECT m.id, m.timestamp, m.role, m.content
                      FROM messages m JOIN target t ON t.session_id = m.session_id
                     WHERE (m.timestamp > t.timestamp OR (m.timestamp = t.timestamp AND m.id > t.id))
                       AND COALESCE(m.active, 1) = 1
                     ORDER BY m.timestamp ASC, m.id ASC LIMIT 1
                )
                """,
                (message_id, message_id),
            ).fetchall()
        return [
            {"role": str(row["role"] or ""), "content": _preview_text(_decode_content(row["content"]), 200)}
            for row in rows
        ]

    def _bookend(
        self,
        session_id: str,
        *,
        before_id: int | None = None,
        after_id: int | None = None,
        limit: int,
        roles: tuple[str, ...] | None,
        include_inactive: bool,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        where = ["session_id = ?"]
        params: list[Any] = [session_id]
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
            order = "id ASC"
        elif after_id is not None:
            where.append("id > ?")
            params.append(after_id)
            order = "id DESC"
        else:
            order = "id ASC"
        if not include_inactive and "active" in self._table_columns("messages"):
            where.append("active = 1")
        if roles is not None:
            placeholders = ",".join("?" for _ in roles)
            where.append(f"role IN ({placeholders})")
            params.extend(roles)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM messages WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                params + [limit],
            ).fetchall()
        shaped = [_message_row(row) for row in rows]
        if after_id is not None:
            shaped.reverse()
        return [row for row in shaped if _preview_text(row.get("content"), 1)]

    def _table_exists(self, table: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    def _table_columns(self, table: str) -> set[str]:
        try:
            return {str(row["name"]) for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            return set()

    def _session_has_messages(self, session_id: str) -> bool:
        if not self._table_exists("messages"):
            return False
        columns = self._table_columns("messages")
        active_clause = " AND active = 1" if "active" in columns else ""
        row = self._conn.execute(
            f"SELECT 1 FROM messages WHERE session_id = ?{active_clause} LIMIT 1",
            (str(session_id or ""),),
        ).fetchone()
        return row is not None

    def _latest_child_session_id(self, session_id: str) -> str:
        if "parent_session_id" not in self._table_columns("sessions"):
            return ""
        row = self._conn.execute(
            """
            SELECT id
              FROM sessions
             WHERE parent_session_id = ?
             ORDER BY started_at DESC, id DESC
             LIMIT 1
            """,
            (str(session_id or ""),),
        ).fetchone()
        return str(row["id"] or "") if row is not None else ""


def unavailable_message() -> str:
    return "Session recall is unavailable because the session read model could not be opened."


def _message_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    if "content" in item:
        item["content"] = _decode_content(item["content"])
    for key in ("tool_calls", "metadata_json"):
        if item.get(key):
            try:
                decoded = json.loads(item[key])
            except (json.JSONDecodeError, TypeError):
                decoded = [] if key == "tool_calls" else None
            if key == "tool_calls":
                item["tool_calls"] = decoded
            else:
                item["metadata"] = decoded
    return item


def _decode_content(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    return decoded


def _preview_text(value: Any, limit: int) -> str:
    if isinstance(value, list):
        parts = [
            str(part.get("text") or "")
            for part in value
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = " ".join(part for part in parts if part).strip() or "[multimodal content]"
    elif isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    else:
        text = str(value)
    return text[:limit]


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _escape_like(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains_cjk(text: str) -> bool:
    return any(
        0x4E00 <= ord(ch) <= 0x9FFF
        or 0x3400 <= ord(ch) <= 0x4DBF
        or 0x20000 <= ord(ch) <= 0x2A6DF
        or 0x3000 <= ord(ch) <= 0x303F
        or 0x3040 <= ord(ch) <= 0x309F
        or 0x30A0 <= ord(ch) <= 0x30FF
        or 0xAC00 <= ord(ch) <= 0xD7AF
        for ch in str(text or "")
    )


def _cjk_like_tokens(query: str) -> list[_SearchToken]:
    raw_tokens = [token for token in str(query or "").strip().split() if token]
    if not raw_tokens:
        return []
    tokens: list[_SearchToken] = []
    pending_operator = "AND"
    for raw in raw_tokens:
        upper = raw.upper()
        if upper in {"AND", "OR", "NOT"}:
            pending_operator = upper
            continue
        tokens.append(_SearchToken(raw.strip('"'), pending_operator))
        pending_operator = "AND"
    if tokens:
        return [token for token in tokens if token.value]
    value = str(query or "").strip().strip('"')
    return [_SearchToken(value, "AND")] if value else []


def _cjk_like_clause() -> str:
    return (
        "(m.content LIKE ? ESCAPE '\\' "
        "OR m.tool_name LIKE ? ESCAPE '\\' "
        "OR m.tool_calls LIKE ? ESCAPE '\\')"
    )


def _cjk_like_predicate(tokens: list[_SearchToken]) -> str:
    positive = [token for token in tokens if token.operator != "NOT"]
    negative = [token for token in tokens if token.operator == "NOT"]
    if positive:
        joiner = " OR " if any(token.operator == "OR" for token in positive) else " AND "
        positive_sql = "(" + joiner.join(_cjk_like_clause() for _ in positive) + ")"
    else:
        positive_sql = "1 = 1"
    if not negative:
        return positive_sql
    negative_sql = " AND ".join(f"NOT {_cjk_like_clause()}" for _ in negative)
    return f"{positive_sql} AND {negative_sql}"


def _sanitize_fts5_query(query: str) -> str:
    quoted_parts: list[str] = []

    def preserve(match: re.Match[str]) -> str:
        quoted_parts.append(match.group(0))
        return f"\x00Q{len(quoted_parts) - 1}\x00"

    sanitized = re.sub(r'"[^"]*"', preserve, str(query or ""))
    sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)
    sanitized = re.sub(r"\*+", "*", sanitized)
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)
    sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
    sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
    for index, quoted in enumerate(quoted_parts):
        sanitized = sanitized.replace(f"\x00Q{index}\x00", quoted)
    return sanitized.strip()


__all__ = [
    "SessionRecallReadModel",
    "SessionRecallUnavailable",
    "unavailable_message",
]
