"""Read model for user-facing session list rows.

This module owns the SQL projection formerly embedded in the legacy state
facade. It is intentionally read-only and returns plain dict rows shaped for
gateway projection/enrichment.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionListQuery:
    source: str | None = None
    exclude_sources: tuple[str, ...] = ()
    limit: int = 20
    offset: int = 0
    include_children: bool = False
    project_compression_tips: bool = True
    order_by_last_active: bool = False
    page_cursor: dict[str, Any] | None = None
    id_query: str | None = None
    min_message_count: int = 0
    archived: str = "false"
    include_internal: bool = False


class SessionListReadModel:
    """SQLite-backed read model for the user-facing session list."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list(self, query: SessionListQuery) -> list[dict[str, Any]]:
        where_clauses: list[str] = []
        params: list[Any] = []

        archived_mode = str(query.archived or "false").strip().lower()
        if archived_mode not in {"false", "true", "only", "all"}:
            raise ValueError(f"unsupported archived filter: {query.archived!r}")
        if not query.include_internal:
            where_clauses.extend(
                [
                    "COALESCE(s.session_kind, 'hermes_session') != 'execution'",
                    "COALESCE(s.conversation_kind, 'direct') IN ('direct', 'team')",
                ]
            )

        if not query.include_children:
            where_clauses.append(
                "(s.parent_session_id IS NULL OR s.parent_session_id = ''"
                " OR EXISTS (SELECT 1 FROM session_lineage l"
                "            WHERE l.session_id = s.id"
                "            AND l.branch_origin = 'user_message_action')"
                " OR EXISTS (SELECT 1 FROM sessions p"
                "            WHERE p.id = s.parent_session_id"
                "            AND p.end_reason = 'branched'"
                "            AND s.started_at >= p.ended_at))"
            )

        if query.source:
            where_clauses.append("s.source = ?")
            params.append(query.source)
        if query.exclude_sources:
            placeholders = ",".join("?" for _ in query.exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(query.exclude_sources)
        if int(query.min_message_count or 0) > 0:
            where_clauses.append("COALESCE(s.message_count, 0) >= ?")
            params.append(max(0, int(query.min_message_count)))
        if archived_mode != "all":
            where_clauses.append("COALESCE(s.archived, 0) = ?")
            params.append(1 if archived_mode in {"true", "only"} else 0)

        id_needle = str(query.id_query or "").strip().lower()
        id_like_pattern = (
            "%"
            + id_needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
            if id_needle
            else ""
        )

        if query.order_by_last_active:
            rows = self._list_ordered_by_activity(
                where_clauses=where_clauses,
                params=params,
                query=query,
                id_needle=id_needle,
                id_like_pattern=id_like_pattern,
            )
        else:
            rows = self._list_ordered_by_start(
                where_clauses=where_clauses,
                params=params,
                query=query,
                id_needle=id_needle,
                id_like_pattern=id_like_pattern,
            )

        sessions = [self._row_to_rich_dict(row) for row in rows]
        if query.project_compression_tips and not query.include_children:
            sessions = [self._project_compression_tip(row) for row in sessions]
        return sessions

    def _list_ordered_by_activity(
        self,
        *,
        where_clauses: list[str],
        params: list[Any],
        query: SessionListQuery,
        id_needle: str,
        id_like_pattern: str,
    ) -> list[sqlite3.Row]:
        seed_where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        outer_where_clauses = list(where_clauses)
        outer_params = list(params)
        cursor_id = _cursor_id(query.page_cursor)
        if cursor_id:
            effective_last_active_expr = (
                "COALESCE(cm.effective_last_active, COALESCE(s.last_active, s.started_at))"
            )
            cursor_effective_last_active = _cursor_number(
                query.page_cursor,
                "effective_last_active",
            )
            cursor_started_at = _cursor_number(query.page_cursor, "started_at")
            outer_where_clauses.append(
                f"""(
                    {effective_last_active_expr} < ?
                    OR ({effective_last_active_expr} = ? AND s.started_at < ?)
                    OR ({effective_last_active_expr} = ? AND s.started_at = ? AND s.id < ?)
                )"""
            )
            outer_params.extend(
                [
                    cursor_effective_last_active,
                    cursor_effective_last_active,
                    cursor_started_at,
                    cursor_effective_last_active,
                    cursor_started_at,
                    cursor_id,
                ]
            )
        if id_needle:
            outer_where_clauses.append(
                "EXISTS (SELECT 1 FROM chain cq "
                "WHERE cq.root_id = s.id "
                "AND LOWER(cq.cur_id) LIKE ? ESCAPE '\\')"
            )
            outer_params.append(id_like_pattern)
        outer_where_sql = (
            f"WHERE {' AND '.join(outer_where_clauses)}"
            if outer_where_clauses
            else ""
        )
        sql = f"""
            WITH RECURSIVE chain(root_id, cur_id) AS (
                SELECT s.id, s.id FROM sessions s {seed_where_sql}
                UNION ALL
                SELECT c.root_id, child.id
                FROM chain c
                JOIN sessions parent ON parent.id = c.cur_id
                JOIN sessions child ON child.parent_session_id = c.cur_id
                WHERE parent.end_reason = 'compression'
                  AND child.started_at >= parent.ended_at
            ),
            chain_max AS (
                SELECT
                    root_id,
                    MAX(COALESCE(ss.last_active, ss.started_at)) AS effective_last_active
                FROM chain c
                JOIN sessions ss ON ss.id = c.cur_id
                GROUP BY root_id
            )
            SELECT s.*,
                COALESCE(s.preview, '') AS _preview_summary,
                COALESCE(s.last_active, s.started_at) AS _last_active_summary,
                COALESCE(cm.effective_last_active, COALESCE(s.last_active, s.started_at))
                    AS _effective_last_active
            FROM sessions s
            LEFT JOIN chain_max cm ON cm.root_id = s.id
            {outer_where_sql}
            ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + outer_params + [_limit(query.limit), max(0, int(query.offset or 0))]
        return self._conn.execute(sql, tuple(final_params)).fetchall()

    def _list_ordered_by_start(
        self,
        *,
        where_clauses: list[str],
        params: list[Any],
        query: SessionListQuery,
        id_needle: str,
        id_like_pattern: str,
    ) -> list[sqlite3.Row]:
        outer_where_clauses = list(where_clauses)
        outer_params = list(params)
        cursor_id = _cursor_id(query.page_cursor)
        if cursor_id:
            cursor_started_at = _cursor_number(query.page_cursor, "started_at")
            outer_where_clauses.append(
                "(s.started_at < ? OR (s.started_at = ? AND s.id < ?))"
            )
            outer_params.extend([cursor_started_at, cursor_started_at, cursor_id])
        if id_needle:
            outer_where_clauses.append("LOWER(s.id) LIKE ? ESCAPE '\\'")
            outer_params.append(id_like_pattern)
        outer_where_sql = (
            f"WHERE {' AND '.join(outer_where_clauses)}"
            if outer_where_clauses
            else ""
        )
        sql = f"""
            SELECT s.*,
                COALESCE(s.preview, '') AS _preview_summary,
                COALESCE(s.last_active, s.started_at) AS _last_active_summary
            FROM sessions s
            {outer_where_sql}
            ORDER BY s.started_at DESC, s.id DESC
            LIMIT ? OFFSET ?
        """
        outer_params.extend([_limit(query.limit), max(0, int(query.offset or 0))])
        return self._conn.execute(sql, tuple(outer_params)).fetchall()

    def _row_to_rich_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["preview"] = str(item.pop("_preview_summary", item.get("preview") or "") or "")
        last_active = item.pop("_last_active_summary", None)
        if last_active is not None:
            item["last_active"] = last_active
        effective_last_active = item.pop("_effective_last_active", None)
        if effective_last_active is None:
            effective_last_active = item.get("last_active") or item.get("started_at") or 0
        item["_page_cursor"] = {
            "effective_last_active": effective_last_active,
            "started_at": item.get("started_at") or 0,
            "id": item.get("id") or "",
        }
        item["archived"] = bool(item.get("archived") or 0)
        return item

    def _project_compression_tip(self, row: dict[str, Any]) -> dict[str, Any]:
        if row.get("end_reason") != "compression":
            return row
        tip_id = self._compression_tip(str(row.get("id") or ""))
        if not tip_id or tip_id == row.get("id"):
            return row
        tip_row = self._rich_row(tip_id)
        if not tip_row:
            return row
        merged = dict(row)
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
        ):
            if key in tip_row:
                merged[key] = tip_row[key]
        merged["_lineage_root_id"] = row.get("id")
        return merged

    def _compression_tip(self, session_id: str) -> str:
        current = str(session_id or "").strip()
        if not current:
            return ""
        while True:
            row = self._conn.execute(
                """
                SELECT child.id
                  FROM sessions parent
                  JOIN sessions child ON child.parent_session_id = parent.id
                 WHERE parent.id = ?
                   AND parent.end_reason = 'compression'
                   AND child.started_at >= parent.ended_at
                 ORDER BY child.started_at DESC, child.id DESC
                 LIMIT 1
                """,
                (current,),
            ).fetchone()
            if row is None:
                return current
            current = str(row["id"] or "")

    def _rich_row(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT s.*,
                   COALESCE(s.preview, '') AS _preview_summary,
                   COALESCE(s.last_active, s.started_at) AS _last_active_summary
              FROM sessions s
             WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_rich_dict(row)


def _cursor_number(cursor: dict[str, Any] | None, key: str) -> float:
    if not cursor:
        return 0.0
    try:
        return float(cursor.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _cursor_id(cursor: dict[str, Any] | None) -> str:
    if not cursor:
        return ""
    value = cursor.get("id")
    return str(value) if value is not None else ""


def _limit(value: int) -> int:
    return max(1, int(value or 20))


__all__ = ["SessionListQuery", "SessionListReadModel"]
