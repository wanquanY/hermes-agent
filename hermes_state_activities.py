"""Activity rows for Option D conversation work orchestration.

The table models user-visible work cards within a conversation. This module is
pure persistence: business code wires activity creation and completion in later
PRs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

_TERMINAL_ACTIVITY_STATUSES = {"completed", "failed", "cancelled"}
_ACTIVITY_STATUS_ALLOWED_PREVIOUS = {
    "running": ("pending",),
    "completed": ("pending", "running"),
    "failed": ("pending", "running"),
    "cancelled": ("pending", "running"),
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _json_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _mission_activity_id(mission_id: str) -> str:
    normalized = _text(mission_id)
    if not normalized:
        raise ValueError("mission_id required")
    return f"mission:{normalized}"


class ActivitiesMixin:
    def create_activity(
        self,
        *,
        activity_id,
        conversation_id,
        kind,
        parent_activity_id=None,
        target_profile_id=None,
        target_team_id=None,
        target_mission_id=None,
        status="pending",
        prompt_summary=None,
        notify_parent=True,
    ) -> dict:
        """Insert an activity row. Returns the row as dict."""
        normalized_activity_id = _text(activity_id)
        normalized_conversation_id = _text(conversation_id)
        normalized_kind = _text(kind)
        normalized_status = _text(status) or "pending"
        if not normalized_activity_id:
            raise ValueError("activity_id required")
        if not normalized_conversation_id:
            raise ValueError("conversation_id required")
        if not normalized_kind:
            raise ValueError("kind required")
        if normalized_status not in {"pending", "running", "completed", "failed", "cancelled"}:
            raise ValueError("invalid activity status")
        now = time.time()
        started_at = now if normalized_status == "running" else None
        completed_at = now if normalized_status in _TERMINAL_ACTIVITY_STATUSES else None

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT INTO activities (
                    activity_id, conversation_id, parent_activity_id, kind,
                    target_profile_id, target_team_id, target_mission_id, status,
                    prompt_summary, result_summary, result_json,
                    started_at, completed_at, notify_parent, read_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    normalized_activity_id,
                    normalized_conversation_id,
                    _optional_text(parent_activity_id),
                    normalized_kind,
                    _optional_text(target_profile_id),
                    _optional_text(target_team_id),
                    _optional_text(target_mission_id),
                    normalized_status,
                    _optional_text(prompt_summary),
                    started_at,
                    completed_at,
                    1 if notify_parent else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM activities WHERE activity_id = ?",
                (normalized_activity_id,),
            ).fetchone()
            return self._activity_row_to_dict(row) if row else {}

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def _ensure_mission_activity_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "running",
        prompt_summary: str | None = None,
        now: float | None = None,
    ) -> dict:
        normalized_conversation_id = _text(conversation_id)
        normalized_mission_id = _text(mission_id)
        if not normalized_conversation_id or not normalized_mission_id:
            return {}
        normalized_status = _text(status) or "running"
        if normalized_status not in {"pending", "running", "completed", "failed", "cancelled"}:
            normalized_status = "running"
        timestamp = float(now if now is not None else time.time())
        activity_id = _mission_activity_id(normalized_mission_id)
        existing = conn.execute(
            """
            SELECT activity_id, status, created_at, started_at
            FROM activities
            WHERE kind = 'mission' AND target_mission_id = ?
            LIMIT 1
            """,
            (normalized_mission_id,),
        ).fetchone()
        existing_activity_id = _text(existing["activity_id"] if isinstance(existing, sqlite3.Row) else "")
        existing_status = _text(existing["status"] if isinstance(existing, sqlite3.Row) else "")
        if existing_activity_id and existing_status in _TERMINAL_ACTIVITY_STATUSES:
            row = conn.execute("SELECT * FROM activities WHERE activity_id = ?", (existing_activity_id,)).fetchone()
            return self._activity_row_to_dict(row)
        row_created_at = float((existing["created_at"] if isinstance(existing, sqlite3.Row) else 0) or timestamp)
        started_at = (
            float((existing["started_at"] if isinstance(existing, sqlite3.Row) else 0) or timestamp)
            if normalized_status == "running"
            else None
        )
        completed_at = timestamp if normalized_status in _TERMINAL_ACTIVITY_STATUSES else None
        conn.execute(
            """
            INSERT INTO activities (
                activity_id, conversation_id, parent_activity_id, kind,
                target_profile_id, target_team_id, target_mission_id, status,
                prompt_summary, result_summary, result_json,
                started_at, completed_at, notify_parent, read_at,
                created_at, updated_at
            )
            VALUES (?, ?, NULL, 'mission', NULL, NULL, ?, ?, ?, NULL, NULL, ?, ?, 1, NULL, ?, ?)
            ON CONFLICT(activity_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                kind = 'mission',
                target_mission_id = excluded.target_mission_id,
                status = CASE
                    WHEN activities.status IN ('completed', 'failed', 'cancelled')
                    THEN activities.status
                    ELSE excluded.status
                END,
                prompt_summary = COALESCE(NULLIF(excluded.prompt_summary, ''), activities.prompt_summary),
                started_at = COALESCE(activities.started_at, excluded.started_at),
                completed_at = CASE
                    WHEN activities.status IN ('completed', 'failed', 'cancelled')
                    THEN activities.completed_at
                    ELSE excluded.completed_at
                END,
                updated_at = excluded.updated_at
            """,
            (
                existing_activity_id or activity_id,
                normalized_conversation_id,
                normalized_mission_id,
                normalized_status,
                _optional_text(prompt_summary),
                started_at,
                completed_at,
                row_created_at,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM activities WHERE kind = 'mission' AND target_mission_id = ?",
            (normalized_mission_id,),
        ).fetchone()
        return self._activity_row_to_dict(row) if row else {}

    def ensure_mission_activity(
        self,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "running",
        prompt_summary: str | None = None,
    ) -> dict:
        """Create or refresh the 1:1 mission activity row."""
        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            return self._ensure_mission_activity_on_conn(
                conn,
                conversation_id=conversation_id,
                mission_id=mission_id,
                status=status,
                prompt_summary=prompt_summary,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def get_activity_for_mission(self, mission_id: str) -> Optional[dict]:
        """Fetch the mission activity row by target mission id."""
        normalized_mission_id = _text(mission_id)
        if not normalized_mission_id:
            return None
        row = self._conn.execute(  # type: ignore[attr-defined]
            """
            SELECT *
            FROM activities
            WHERE kind = 'mission' AND target_mission_id = ?
            ORDER BY created_at ASC, activity_id ASC
            LIMIT 1
            """,
            (normalized_mission_id,),
        ).fetchone()
        return self._activity_row_to_dict(row) if row else None

    def list_active_mission_activities(self, conversation_id: str) -> list[dict]:
        """List non-terminal mission activities for a conversation."""
        normalized_conversation_id = _text(conversation_id)
        if not normalized_conversation_id:
            return []
        rows = self._conn.execute(  # type: ignore[attr-defined]
            """
            SELECT *
            FROM activities
            WHERE conversation_id = ?
              AND kind = 'mission'
              AND status NOT IN ('completed', 'failed', 'cancelled')
            ORDER BY started_at ASC, created_at ASC, activity_id ASC
            """,
            (normalized_conversation_id,),
        ).fetchall()
        return [self._activity_row_to_dict(row) for row in rows]

    def _mark_mission_activity_terminal_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        mission_id: str,
        status: str,
        result_summary: str | None = None,
        result_json: Any = None,
        now: float | None = None,
    ) -> bool:
        normalized_mission_id = _text(mission_id)
        normalized_status = _text(status)
        if not normalized_mission_id or normalized_status not in _TERMINAL_ACTIVITY_STATUSES:
            return False
        timestamp = float(now if now is not None else time.time())
        cursor = conn.execute(
            """
            UPDATE activities
               SET status = ?,
                   result_summary = COALESCE(?, result_summary),
                   result_json = COALESCE(?, result_json),
                   completed_at = COALESCE(completed_at, ?),
                   updated_at = ?
             WHERE kind = 'mission'
               AND target_mission_id = ?
               AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (
                normalized_status,
                _optional_text(result_summary),
                _json_text(result_json) if result_json is not None else None,
                timestamp,
                timestamp,
                normalized_mission_id,
            ),
        )
        return bool(cursor.rowcount)

    def update_activity_status(
        self,
        activity_id,
        status,
        *,
        target_profile_id=None,
        target_team_id=None,
        target_mission_id=None,
        result_summary=None,
        result_json=None,
        started_at=None,
        completed_at=None,
    ) -> bool:
        """Update status + optional fields. Returns True if row updated."""
        normalized_activity_id = _text(activity_id)
        normalized_status = _text(status)
        if not normalized_activity_id:
            raise ValueError("activity_id required")
        if not normalized_status:
            raise ValueError("status required")
        now = time.time()
        assignments = ["status = ?", "updated_at = ?"]
        params: List[Any] = [normalized_status, now]
        if target_profile_id is not None:
            assignments.append("target_profile_id = ?")
            params.append(_optional_text(target_profile_id))
        if target_team_id is not None:
            assignments.append("target_team_id = ?")
            params.append(_optional_text(target_team_id))
        if target_mission_id is not None:
            assignments.append("target_mission_id = ?")
            params.append(_optional_text(target_mission_id))
        if result_summary is not None:
            assignments.append("result_summary = ?")
            params.append(_optional_text(result_summary))
        if result_json is not None:
            assignments.append("result_json = ?")
            params.append(_json_text(result_json))
        if started_at is not None:
            assignments.append("started_at = ?")
            params.append(float(started_at))
        if completed_at is not None:
            assignments.append("completed_at = ?")
            params.append(float(completed_at))
        params.append(normalized_activity_id)
        allowed_previous = _ACTIVITY_STATUS_ALLOWED_PREVIOUS.get(normalized_status)

        def _do(conn: sqlite3.Connection) -> bool:
            where = "activity_id = ?"
            update_params = list(params)
            if allowed_previous is not None:
                placeholders = ", ".join("?" for _ in allowed_previous)
                where = f"{where} AND status IN ({placeholders})"
                update_params.extend(allowed_previous)
            cursor = conn.execute(
                f"UPDATE activities SET {', '.join(assignments)} WHERE {where}",
                tuple(update_params),
            )
            if cursor.rowcount > 0:
                return True
            if allowed_previous is not None:
                row = conn.execute(
                    "SELECT status FROM activities WHERE activity_id = ?",
                    (normalized_activity_id,),
                ).fetchone()
                if row is not None:
                    current = row["status"] if isinstance(row, sqlite3.Row) else row[0]
                    _log.warning(
                        "activity status transition rejected activity_id=%s current=%s requested=%s",
                        normalized_activity_id,
                        current,
                        normalized_status,
                    )
            return False

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def mark_activity_completed(self, activity_id, *, result_summary, result_json) -> bool:
        """Shortcut: status=completed + result fields + completed_at=now."""
        return self.update_activity_status(
            activity_id,
            "completed",
            result_summary=result_summary,
            result_json=result_json,
            completed_at=time.time(),
        )

    def mark_activity_failed(
        self, activity_id, *, error_message, result_json=None
    ) -> bool:
        """Shortcut: status=failed + result_summary=error + completed_at=now."""
        return self.update_activity_status(
            activity_id,
            "failed",
            result_summary=error_message,
            result_json=result_json,
            completed_at=time.time(),
        )

    def mark_activity_cancelled(
        self, activity_id, *, result_summary=None, result_json=None
    ) -> bool:
        """Shortcut: status=cancelled + completed_at=now."""
        return self.update_activity_status(
            activity_id,
            "cancelled",
            result_summary=result_summary,
            result_json=result_json,
            completed_at=time.time(),
        )

    def mark_activity_read(self, activity_id) -> bool:
        """Set read_at=now (UI viewed the completion)."""
        normalized_activity_id = _text(activity_id)
        if not normalized_activity_id:
            raise ValueError("activity_id required")
        now = time.time()

        def _do(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                """
                UPDATE activities
                   SET read_at = ?, updated_at = ?
                 WHERE activity_id = ?
                """,
                (now, now, normalized_activity_id),
            )
            return cursor.rowcount > 0

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def list_activities(self, conversation_id, *, status=None, limit=None) -> list[dict]:
        """List activities for a conversation, optional status filter."""
        normalized_conversation_id = _text(conversation_id)
        if not normalized_conversation_id:
            return []
        params: List[Any] = [normalized_conversation_id]
        where = ["conversation_id = ?"]
        if status is not None:
            where.append("status = ?")
            params.append(_text(status))
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM activities "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at ASC, activity_id ASC"
            f"{limit_sql}",
            tuple(params),
        ).fetchall()
        return [self._activity_row_to_dict(row) for row in rows]

    def list_unread_completions(
        self,
        parent_activity_id=None,
        *,
        conversation_id=None,
    ) -> list[dict]:
        """List completed/failed activities with read_at IS NULL.
        Filter by parent (for leader inject) or by conversation (for sidebar count)."""
        where = ["status IN ('completed', 'failed')", "read_at IS NULL"]
        params: List[Any] = []
        if parent_activity_id is not None:
            where.append("parent_activity_id = ?")
            params.append(_text(parent_activity_id))
        if conversation_id is not None:
            where.append("conversation_id = ?")
            params.append(_text(conversation_id))
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM activities "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(completed_at, updated_at) ASC, activity_id ASC",
            tuple(params),
        ).fetchall()
        return [self._activity_row_to_dict(row) for row in rows]

    def get_unread_completion_count(
        self,
        *,
        parent_activity_id=None,
        conversation_id=None,
    ) -> int:
        """Count unread completions; cheap query for sidebar badge."""
        where = ["status IN ('completed', 'failed')", "read_at IS NULL"]
        params: List[Any] = []
        if parent_activity_id is not None:
            where.append("parent_activity_id = ?")
            params.append(_text(parent_activity_id))
        if conversation_id is not None:
            where.append("conversation_id = ?")
            params.append(_text(conversation_id))
        row = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(1) AS count FROM activities "
            f"WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()
        if row is None:
            return 0
        return int(row["count"] if isinstance(row, sqlite3.Row) else row[0])

    def get_activity(self, activity_id) -> Optional[dict]:
        """Fetch one row by id; None if absent."""
        normalized_activity_id = _text(activity_id)
        if not normalized_activity_id:
            return None
        row = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM activities WHERE activity_id = ?",
            (normalized_activity_id,),
        ).fetchone()
        return self._activity_row_to_dict(row) if row else None

    def _activity_row_to_dict(self, row: sqlite3.Row | None) -> dict:
        if row is None:
            return {}
        item = dict(row)
        item["notify_parent"] = bool(item.get("notify_parent"))
        return item
