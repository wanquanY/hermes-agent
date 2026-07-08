from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *


_CONVERSATION_MISSION_STATUSES = {"active", "completed", "cancelled", "failed"}


def _conversation_mission_status(value: str | None) -> str:
    status = _text(value).lower()
    if status == "canceled":
        return "cancelled"
    if status in _CONVERSATION_MISSION_STATUSES:
        return status
    return "active"


class TeamMissionConversationMissionMixin:
    def _activity_conversation_id_for_mission_on_conn(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
    ) -> str:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return ""
        row = conn.execute(
            """
            SELECT stable_session_id
            FROM team_mission_conversations
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        stable_session_id = _text(_row_value(row, "stable_session_id", ""))
        return stable_session_id or conversation_id

    def _sync_mission_activity_for_conversation_mission_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        mission_id: str,
        status: str,
        now: float | None = None,
    ) -> None:
        conversation_id = _text(conversation_id)
        mission_id = _text(mission_id)
        if not conversation_id or not mission_id:
            return
        normalized_status = _conversation_mission_status(status)
        activity_conversation_id = self._activity_conversation_id_for_mission_on_conn(conn, conversation_id)
        if normalized_status == "active":
            self._ensure_mission_activity_on_conn(
                conn,
                conversation_id=activity_conversation_id,
                mission_id=mission_id,
                status="running",
                now=now,
            )
            return
        activity_status = "cancelled" if normalized_status == "cancelled" else normalized_status
        self._mark_mission_activity_terminal_on_conn(
            conn,
            mission_id=mission_id,
            status=activity_status,
            result_summary=f"Mission {activity_status}",
            now=now,
        )

    def _conversation_mission_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any] | None:
        if row is None:
            return None
        metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
        return {
            "conversation_id": _text(_row_value(row, "conversation_id", "")),
            "mission_id": _text(_row_value(row, "mission_id", "")),
            "status": _conversation_mission_status(_row_value(row, "status", "active")),
            "added_at": float(_row_value(row, "added_at", 0) or 0),
            "updated_at": float(_row_value(row, "updated_at", 0) or 0),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    def _latest_active_conversation_mission_id_on_conn(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
    ) -> str:
        row = conn.execute(
            """
            SELECT mission_id
            FROM conversation_missions
            WHERE conversation_id = ?
              AND status = 'active'
            ORDER BY updated_at DESC, added_at DESC, mission_id DESC
            LIMIT 1
            """,
            (_text(conversation_id),),
        ).fetchone()
        return _text(_row_value(row, "mission_id", ""))

    def _add_mission_to_conversation_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "active",
        metadata: Dict[str, Any] | None = None,
        now: float | None = None,
    ) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        mission_id = _text(mission_id)
        if not conversation_id or not mission_id:
            return {}
        normalized_status = _conversation_mission_status(status)
        timestamp = float(now if now is not None else time.time())
        existing = conn.execute(
            """
            SELECT metadata_json, added_at
            FROM conversation_missions
            WHERE conversation_id = ? AND mission_id = ?
            """,
            (conversation_id, mission_id),
        ).fetchone()
        existing_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
        merged_metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        metadata_json = _json_dumps(merged_metadata) if merged_metadata else ""
        conn.execute(
            """
            INSERT INTO conversation_missions (
                conversation_id, mission_id, status, added_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, mission_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                conversation_id,
                mission_id,
                normalized_status,
                float(_row_value(existing, "added_at", timestamp) or timestamp),
                timestamp,
                metadata_json,
            ),
        )
        self._sync_mission_activity_for_conversation_mission_on_conn(
            conn,
            conversation_id=conversation_id,
            mission_id=mission_id,
            status=normalized_status,
            now=timestamp,
        )
        return self._conversation_mission_from_row(conn.execute(
            """
            SELECT *
            FROM conversation_missions
            WHERE conversation_id = ? AND mission_id = ?
            """,
            (conversation_id, mission_id),
        ).fetchone()) or {}

    def add_mission_to_conversation(
        self,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "active",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            return self._add_mission_to_conversation_on_conn(
                conn,
                conversation_id=conversation_id,
                mission_id=mission_id,
                status=status,
                metadata=metadata,
            )

        return self._execute_write(_do)

    def set_conversation_mission_status(
        self,
        *,
        conversation_id: str,
        mission_id: str,
        status: str,
    ) -> Dict[str, Any] | None:
        conversation_id = _text(conversation_id)
        mission_id = _text(mission_id)
        if not conversation_id or not mission_id:
            return None
        normalized_status = _conversation_mission_status(status)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any] | None:
            result = conn.execute(
                """
                UPDATE conversation_missions
                SET status = ?, updated_at = ?
                WHERE conversation_id = ? AND mission_id = ?
                """,
                (normalized_status, now, conversation_id, mission_id),
            )
            if not result.rowcount:
                return None
            self._sync_mission_activity_for_conversation_mission_on_conn(
                conn,
                conversation_id=conversation_id,
                mission_id=mission_id,
                status=normalized_status,
                now=now,
            )
            return self._conversation_mission_from_row(conn.execute(
                """
                SELECT *
                FROM conversation_missions
                WHERE conversation_id = ? AND mission_id = ?
                """,
                (conversation_id, mission_id),
            ).fetchone())

        return self._execute_write(_do)

    def list_conversation_missions(
        self,
        conversation_id: str,
        status: str | None = None,
    ) -> List[Dict[str, Any]]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return []
        params: list[Any] = [conversation_id]
        status_clause = ""
        if status is not None:
            status_clause = " AND status = ?"
            params.append(_conversation_mission_status(status))
        rows = self._conn.execute(
            f"""
            SELECT *
            FROM conversation_missions
            WHERE conversation_id = ?
            {status_clause}
            ORDER BY added_at DESC, updated_at DESC, mission_id DESC
            """,
            tuple(params),
        ).fetchall()
        return [
            item
            for item in (self._conversation_mission_from_row(row) for row in rows)
            if item is not None
        ]

    def has_active_mission(self, conversation_id: str) -> bool:
        return bool(self.list_conversation_missions(conversation_id, status="active"))

    def active_mission_ids(self, conversation_id: str) -> list[str]:
        return [
            mission["mission_id"]
            for mission in self.list_conversation_missions(conversation_id, status="active")
        ]

    def remove_mission_from_conversation(
        self,
        *,
        conversation_id: str,
        mission_id: str,
    ) -> bool:
        conversation_id = _text(conversation_id)
        mission_id = _text(mission_id)
        if not conversation_id or not mission_id:
            return False

        def _do(conn: sqlite3.Connection) -> bool:
            result = conn.execute(
                """
                DELETE FROM conversation_missions
                WHERE conversation_id = ? AND mission_id = ?
                """,
                (conversation_id, mission_id),
            )
            removed = bool(result.rowcount)
            return removed

        return self._execute_write(_do)
