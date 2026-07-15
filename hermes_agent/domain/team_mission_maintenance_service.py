"""Transactional maintenance operations for Team Mission state."""

from __future__ import annotations

from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class TeamMissionMaintenanceService:
    def __init__(
        self,
        repository: TeamMissionRepoImpl,
        unit_of_work: SqliteUnitOfWork,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work

    def rebase_workspace_paths(self, old_path: str, new_path: str) -> dict[str, int]:
        source = str(old_path or "").strip()
        target = str(new_path or "").strip()
        if not source or not target or source == target:
            return {
                "team_missions": 0,
                "team_mission_conversations": 0,
            }
        return self._unit_of_work.execute(
            lambda _conn: self._repository.rebase_workspace_paths(source, target)
        )


__all__ = ["TeamMissionMaintenanceService"]
