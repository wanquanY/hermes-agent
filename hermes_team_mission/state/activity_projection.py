"""Team Mission activity projection helpers."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

_TERMINAL_ACTIVITY_STATUSES = {"completed", "failed", "cancelled"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _json_text(value: Any) -> str | None:
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


class TeamMissionActivityProjectionMixin:
    def _ensure_mission_activity_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "running",
        prompt_summary: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
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

    def ensure_mission_activity(
        self,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "running",
        prompt_summary: str | None = None,
    ) -> dict[str, Any]:
        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            return self._ensure_mission_activity_on_conn(
                conn,
                conversation_id=conversation_id,
                mission_id=mission_id,
                status=status,
                prompt_summary=prompt_summary,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def _activity_row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        item["notify_parent"] = bool(item.get("notify_parent"))
        return item
