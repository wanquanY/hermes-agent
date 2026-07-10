"""Transactional boundary for the Team Mission audit projection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hermes_agent.domain.team_mission_audit_log import (
    AuditAppendResult,
    TeamMissionAuditLog,
)
from hermes_agent.storage.unit_of_work import LockLike, SqliteUnitOfWork


class TeamMissionAuditService:
    def __init__(
        self,
        audit_log: TeamMissionAuditLog,
        unit_of_work: SqliteUnitOfWork,
        lock: LockLike,
    ) -> None:
        self._audit_log = audit_log
        self._unit_of_work = unit_of_work
        self._lock = lock

    def append(self, **fields: Any) -> AuditAppendResult:
        return self._unit_of_work.execute(
            lambda _conn: self._audit_log.append(**fields)
        )

    def list(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return self._audit_log.list(
                mission_id,
                after_seq=after_seq,
                limit=limit,
            )

    def latest_seq(self, mission_id: str) -> int:
        with self._lock:
            return self._audit_log.latest_seq(mission_id)

    def prune_source_event_types(
        self,
        *,
        mission_id: str,
        source_event_types: Iterable[str],
    ) -> int:
        return self._unit_of_work.execute(
            lambda _conn: self._audit_log.prune_source_event_types(
                mission_id=mission_id,
                source_event_types=source_event_types,
            )
        )


__all__ = ["TeamMissionAuditService"]
