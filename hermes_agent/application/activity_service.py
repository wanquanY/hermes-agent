"""Activity lifecycle application service."""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes_agent.application.mission_activity_reconciler import MissionActivityReconciler
from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class ActivityService:
    def __init__(
        self,
        repository: TeamMissionRepoImpl,
        unit_of_work: SqliteUnitOfWork,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work

    def create(self, **fields: Any) -> dict[str, Any]:
        return self._write(lambda: self._repository.create_legacy_activity(**fields))

    def ensure_mission(self, **fields: Any) -> dict[str, Any]:
        return self._write(lambda: self._repository.ensure_legacy_mission_activity(**fields))

    def bind_to_mission(self, **fields: Any) -> dict[str, Any]:
        return self._write(lambda: self._repository.bind_legacy_activity_to_mission(**fields))

    def get_for_mission(self, mission_id: str) -> dict[str, Any] | None:
        return self._read(
            lambda: self._repository.get_legacy_activity_for_mission(mission_id)
        )

    def list_active_missions(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._read(
            lambda: self._repository.list_legacy_active_mission_activities(
                conversation_id
            )
        )

    def mark_mission_terminal(self, **fields: Any) -> bool:
        return self._write(
            lambda: self._repository.mark_legacy_mission_activity_terminal(**fields)
        )

    def update_status(self, activity_id: str, status: str, **fields: Any) -> bool:
        return self._write(
            lambda: self._repository.update_legacy_activity_status(
                activity_id,
                status,
                **fields,
            )
        )

    def mark_completed(
        self,
        activity_id: str,
        *,
        result_summary: str,
        result_json: Any,
    ) -> bool:
        return self._write(
            lambda: self._repository.mark_legacy_activity_completed(
                activity_id,
                result_summary=result_summary,
                result_json=result_json,
            )
        )

    def mark_failed(
        self,
        activity_id: str,
        *,
        error_message: str,
        result_json: Any = None,
    ) -> bool:
        return self._write(
            lambda: self._repository.mark_legacy_activity_failed(
                activity_id,
                error_message=error_message,
                result_json=result_json,
            )
        )

    def mark_cancelled(
        self,
        activity_id: str,
        *,
        result_summary: str | None = None,
        result_json: Any = None,
    ) -> bool:
        return self._write(
            lambda: self._repository.mark_legacy_activity_cancelled(
                activity_id,
                result_summary=result_summary,
                result_json=result_json,
            )
        )

    def mark_read(self, activity_id: str) -> bool:
        return self._write(lambda: self._repository.mark_legacy_activity_read(activity_id))

    def list(
        self,
        conversation_id: str,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._read(
            lambda: self._repository.list_legacy_activities(
                conversation_id,
                status=status,
                limit=limit,
            )
        )

    def list_unread(
        self,
        parent_activity_id: str | None = None,
        *,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._read(
            lambda: self._repository.list_legacy_unread_completions(
                parent_activity_id,
                conversation_id=conversation_id,
            )
        )

    def unread_count(
        self,
        *,
        parent_activity_id: str | None = None,
        conversation_id: str | None = None,
    ) -> int:
        return self._read(
            lambda: self._repository.get_legacy_unread_completion_count(
                parent_activity_id=parent_activity_id,
                conversation_id=conversation_id,
            )
        )

    def get(self, activity_id: str) -> dict[str, Any] | None:
        return self._read(lambda: self._repository.get_legacy_activity(activity_id))

    def insert_command(self, **fields: Any) -> dict[str, Any]:
        return self._write(lambda: self._repository.insert_activity_command(**fields))

    def get_command(self, command_id: str) -> dict[str, Any]:
        return self._read(lambda: self._repository.get_activity_command(command_id))

    def list_pending_commands(self, **query: Any) -> list[dict[str, Any]]:
        return self._read(
            lambda: self._repository.list_pending_activity_commands(**query)
        )

    def update_command_state(self, command_id: str, **fields: Any) -> dict[str, Any]:
        return self._write(
            lambda: self._repository.update_activity_command_state(command_id, **fields)
        )

    def list_commands(self, activity_id: str, **query: Any) -> list[dict[str, Any]]:
        return self._read(
            lambda: self._repository.list_activity_commands_for_activity(
                activity_id,
                **query,
            )
        )

    def reconcile_missions_once(self) -> dict[str, Any]:
        return self._unit_of_work.execute(
            lambda conn: MissionActivityReconciler(conn).reconcile()
        )

    def _write(self, operation):
        return self._unit_of_work.execute(lambda _conn: operation())

    def _read(self, operation):
        return self._unit_of_work.read(lambda _conn: operation())


__all__ = ["ActivityService"]
