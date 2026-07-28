"""Application policy for persisted compression and stream stability state."""

from __future__ import annotations

import time
from collections.abc import Callable

from hermes_agent.domain.runtime_stability import SessionRuntimeStability
from hermes_agent.repositories.runtime_stability_repo import (
    RuntimeStabilityRepository,
)
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class SessionRuntimeStabilityService:
    def __init__(
        self,
        repository: RuntimeStabilityRepository,
        unit_of_work: SqliteUnitOfWork,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._clock = clock

    @staticmethod
    def _session_id(value: str) -> str:
        session_id = str(value or "").strip()
        if not session_id:
            raise ValueError("runtime stability operation requires session_id")
        return session_id

    def get(self, session_id: str) -> SessionRuntimeStability:
        normalized = self._session_id(session_id)
        state = self._unit_of_work.read(
            lambda _conn: self._repository.get(normalized)
        )
        return state or SessionRuntimeStability.empty(normalized)

    def write_compression(
        self,
        session_id: str,
        *,
        ineffective_count: int,
        fallback_streak: int,
        verdict_pending: bool,
        cooldown_until: float = 0.0,
        error: str = "",
    ) -> None:
        normalized = self._session_id(session_id)
        ineffective = max(0, int(ineffective_count))
        fallback = max(0, int(fallback_streak))
        now = float(self._clock())
        self._unit_of_work.execute(
            lambda _conn: self._repository.write_compression(
                normalized,
                ineffective_count=ineffective,
                fallback_streak=fallback,
                verdict_pending=bool(verdict_pending),
                cooldown_until=max(0.0, float(cooldown_until)),
                error=str(error or "")[:500],
                updated_at=now,
            )
        )

    def record_stream_stale_failure(
        self,
        session_id: str,
        *,
        route_hash: str,
        threshold: int,
        open_seconds: float,
        error: str,
    ) -> SessionRuntimeStability:
        normalized = self._session_id(session_id)
        normalized_route = str(route_hash or "").strip()
        if not normalized_route:
            raise ValueError("stream stale failure requires route_hash")
        limit = max(1, int(threshold))
        now = float(self._clock())
        retry_after = now + max(1.0, float(open_seconds))
        return self._unit_of_work.execute(
            lambda _conn: self._repository.record_stream_stale_failure(
                normalized,
                route_hash=normalized_route,
                threshold=limit,
                retry_after=retry_after,
                error=str(error or "")[:500],
                updated_at=now,
            )
        )

    def clear_stream_stale(self, session_id: str) -> None:
        normalized = self._session_id(session_id)
        now = float(self._clock())
        self._unit_of_work.execute(
            lambda _conn: self._repository.clear_stream_stale(
                normalized,
                updated_at=now,
            )
        )

    def carry_forward(self, old_session_id: str, new_session_id: str) -> None:
        old_normalized = self._session_id(old_session_id)
        new_normalized = self._session_id(new_session_id)
        if old_normalized == new_normalized:
            return
        now = float(self._clock())
        self._unit_of_work.execute(
            lambda _conn: self._repository.carry_forward(
                old_normalized,
                new_normalized,
                updated_at=now,
            )
        )


__all__ = ["SessionRuntimeStabilityService"]
