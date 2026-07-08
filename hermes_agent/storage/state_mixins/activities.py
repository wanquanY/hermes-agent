"""Legacy activity facade backed by TeamMissionRepo.

The legacy method surface still exists for gateway callers during P2, but
physical writes to ``activities`` now belong to TeamMissionRepoImpl.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl


class ActivitiesMixin:
    def _activity_repo(self, conn: sqlite3.Connection | None = None) -> TeamMissionRepoImpl:
        return TeamMissionRepoImpl(conn or self._conn)  # type: ignore[attr-defined]

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
        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            return self._activity_repo(conn).create_legacy_activity(
                activity_id=str(activity_id or ""),
                conversation_id=str(conversation_id or ""),
                kind=str(kind or ""),
                parent_activity_id=parent_activity_id,
                target_profile_id=target_profile_id,
                target_team_id=target_team_id,
                target_mission_id=target_mission_id,
                status=str(status or "pending"),
                prompt_summary=prompt_summary,
                notify_parent=bool(notify_parent),
            )

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
        return self._activity_repo(conn).ensure_legacy_mission_activity(
            conversation_id=conversation_id,
            mission_id=mission_id,
            status=status,
            prompt_summary=prompt_summary,
            now=now,
        )

    def ensure_mission_activity(
        self,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "running",
        prompt_summary: str | None = None,
    ) -> dict:
        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            return self._activity_repo(conn).ensure_legacy_mission_activity(
                conversation_id=conversation_id,
                mission_id=mission_id,
                status=status,
                prompt_summary=prompt_summary,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def bind_activity_to_mission(
        self,
        *,
        activity_id: str,
        conversation_id: str,
        mission_id: str,
        target_team_id: str | None = None,
        prompt_summary: str | None = None,
        status: str = "running",
    ) -> dict:
        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            return self._activity_repo(conn).bind_legacy_activity_to_mission(
                activity_id=activity_id,
                conversation_id=conversation_id,
                mission_id=mission_id,
                target_team_id=target_team_id,
                prompt_summary=prompt_summary,
                status=status,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def get_activity_for_mission(self, mission_id: str) -> dict | None:
        return self._activity_repo().get_legacy_activity_for_mission(mission_id)

    def list_active_mission_activities(self, conversation_id: str) -> list[dict]:
        return self._activity_repo().list_legacy_active_mission_activities(conversation_id)

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
        return self._activity_repo(conn).mark_legacy_mission_activity_terminal(
            mission_id=mission_id,
            status=status,
            result_summary=result_summary,
            result_json=result_json,
            now=now,
        )

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
        def _do(conn: sqlite3.Connection) -> bool:
            return self._activity_repo(conn).update_legacy_activity_status(
                str(activity_id or ""),
                str(status or ""),
                target_profile_id=target_profile_id,
                target_team_id=target_team_id,
                target_mission_id=target_mission_id,
                result_summary=result_summary,
                result_json=result_json,
                started_at=started_at,
                completed_at=completed_at,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def mark_activity_completed(self, activity_id, *, result_summary, result_json) -> bool:
        def _do(conn: sqlite3.Connection) -> bool:
            return self._activity_repo(conn).mark_legacy_activity_completed(
                str(activity_id or ""),
                result_summary=result_summary,
                result_json=result_json,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def mark_activity_failed(
        self,
        activity_id,
        *,
        error_message,
        result_json=None,
    ) -> bool:
        def _do(conn: sqlite3.Connection) -> bool:
            return self._activity_repo(conn).mark_legacy_activity_failed(
                str(activity_id or ""),
                error_message=error_message,
                result_json=result_json,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def mark_activity_cancelled(
        self,
        activity_id,
        *,
        result_summary=None,
        result_json=None,
    ) -> bool:
        def _do(conn: sqlite3.Connection) -> bool:
            return self._activity_repo(conn).mark_legacy_activity_cancelled(
                str(activity_id or ""),
                result_summary=result_summary,
                result_json=result_json,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def mark_activity_read(self, activity_id) -> bool:
        def _do(conn: sqlite3.Connection) -> bool:
            return self._activity_repo(conn).mark_legacy_activity_read(str(activity_id or ""))

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def list_activities(self, conversation_id, *, status=None, limit=None) -> list[dict]:
        return self._activity_repo().list_legacy_activities(
            str(conversation_id or ""),
            status=status,
            limit=limit,
        )

    def list_unread_completions(
        self,
        parent_activity_id=None,
        *,
        conversation_id=None,
    ) -> list[dict]:
        return self._activity_repo().list_legacy_unread_completions(
            parent_activity_id=parent_activity_id,
            conversation_id=conversation_id,
        )

    def get_unread_completion_count(
        self,
        *,
        parent_activity_id=None,
        conversation_id=None,
    ) -> int:
        return self._activity_repo().get_legacy_unread_completion_count(
            parent_activity_id=parent_activity_id,
            conversation_id=conversation_id,
        )

    def get_activity(self, activity_id) -> dict | None:
        return self._activity_repo().get_legacy_activity(str(activity_id or ""))

    def insert_activity_command(
        self,
        *,
        command_id: str,
        activity_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            return self._activity_repo(conn).insert_activity_command(
                command_id=command_id,
                activity_id=activity_id,
                kind=kind,
                payload=payload,
                metadata=metadata,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def get_activity_command(self, command_id: str) -> dict[str, Any]:
        normalized_command_id = str(command_id or "").strip()
        if not normalized_command_id:
            return {}
        return self._activity_repo().get_activity_command(normalized_command_id)

    def list_pending_activity_commands(
        self,
        *,
        states: tuple[str, ...] = ("accepted", "dispatched"),
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        normalized_states = tuple(
            state
            for state in (str(value or "").strip() for value in (states or ()))
            if state
        )
        if not normalized_states:
            return []
        return self._activity_repo().list_pending_activity_commands(
            states=normalized_states,
            limit=limit,
        )

    def update_activity_command_state(
        self,
        command_id: str,
        *,
        next_state: str,
        error_reason: str = "",
        result_event_id: int | None = None,
    ) -> dict[str, Any]:
        normalized_command_id = str(command_id or "").strip()
        normalized_next_state = str(next_state or "").strip()
        if not normalized_command_id or not normalized_next_state:
            return {}

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            return self._activity_repo(conn).update_activity_command_state(
                normalized_command_id,
                next_state=normalized_next_state,
                error_reason=error_reason,
                result_event_id=result_event_id,
            )

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def list_activity_commands_for_activity(
        self,
        activity_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        normalized_activity_id = str(activity_id or "").strip()
        if not normalized_activity_id:
            return []
        return self._activity_repo().list_activity_commands_for_activity(
            normalized_activity_id,
            limit=limit,
        )
