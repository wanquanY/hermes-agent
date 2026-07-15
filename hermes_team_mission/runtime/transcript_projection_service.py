"""Application service for repairing canonical team transcript projections."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hermes_agent.storage.unit_of_work import SqliteUnitOfWork
from hermes_team_mission.runtime.team_transcript_writer import (
    backfill_projected_message_artifacts_locked,
    backfill_unprojected_message_complete_events_locked,
)


class TeamTranscriptProjectionService:
    """Owns idempotent repair of legacy raw team transcript events."""

    def __init__(self, store: Any, unit_of_work: SqliteUnitOfWork) -> None:
        self._store = store
        self._unit_of_work = unit_of_work

    def backfill(
        self,
        session_ids: Iterable[str],
        *,
        message_limit: int = 200,
        artifact_limit: int = 500,
    ) -> dict[str, int]:
        targets = list(dict.fromkeys(
            str(session_id or "").strip()
            for session_id in session_ids
            if str(session_id or "").strip()
        ))
        if not targets:
            return {"projected_messages": 0, "merged_artifacts": 0}

        def operation(conn) -> dict[str, int]:
            projected_messages = backfill_unprojected_message_complete_events_locked(
                self._store,
                conn,
                session_ids=targets,
                limit=message_limit,
            )
            merged_artifacts = backfill_projected_message_artifacts_locked(
                self._store,
                conn,
                session_ids=targets,
                limit=artifact_limit,
            )
            return {
                "projected_messages": int(projected_messages or 0),
                "merged_artifacts": int(merged_artifacts or 0),
            }

        return self._unit_of_work.execute(operation)


__all__ = ["TeamTranscriptProjectionService"]
