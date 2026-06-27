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
        prompt_summary=None,
        notify_parent=True,
    ) -> dict:
        """Insert pending activity. Returns the row as dict."""
        normalized_activity_id = _text(activity_id)
        normalized_conversation_id = _text(conversation_id)
        normalized_kind = _text(kind)
        if not normalized_activity_id:
            raise ValueError("activity_id required")
        if not normalized_conversation_id:
            raise ValueError("conversation_id required")
        if not normalized_kind:
            raise ValueError("kind required")
        now = time.time()

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
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)
                """,
                (
                    normalized_activity_id,
                    normalized_conversation_id,
                    _optional_text(parent_activity_id),
                    normalized_kind,
                    _optional_text(target_profile_id),
                    _optional_text(target_team_id),
                    _optional_text(target_mission_id),
                    _optional_text(prompt_summary),
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

    def mark_activity_failed(self, activity_id, *, error_message) -> bool:
        """Shortcut: status=failed + result_summary=error + completed_at=now."""
        return self.update_activity_status(
            activity_id,
            "failed",
            result_summary=error_message,
            completed_at=time.time(),
        )

    def mark_activity_cancelled(self, activity_id) -> bool:
        """Shortcut: status=cancelled + completed_at=now."""
        return self.update_activity_status(
            activity_id,
            "cancelled",
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
