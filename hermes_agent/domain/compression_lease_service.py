"""Application service for atomic session compression leases."""

from __future__ import annotations

import time
from collections.abc import Callable

from hermes_agent.repositories.compression_lease_repo import CompressionLeaseRepository
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class CompressionLeaseService:
    """Coordinates cross-store and cross-process compression exclusion."""

    def __init__(
        self,
        repository: CompressionLeaseRepository,
        unit_of_work: SqliteUnitOfWork,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._clock = clock

    def try_acquire(
        self,
        session_id: str,
        holder: str,
        ttl_seconds: float = 300.0,
    ) -> bool:
        lease_session_id = str(session_id or "").strip()
        lease_holder = str(holder or "").strip()
        if not lease_session_id or not lease_holder:
            return False
        ttl = float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("compression lease ttl_seconds must be positive")
        now = float(self._clock())
        return self._unit_of_work.execute(
            lambda _conn: self._repository.try_acquire(
                lease_session_id,
                lease_holder,
                now=now,
                expires_at=now + ttl,
            )
        )

    def release(self, session_id: str, holder: str) -> bool:
        lease_session_id = str(session_id or "").strip()
        lease_holder = str(holder or "").strip()
        if not lease_session_id or not lease_holder:
            return False
        return self._unit_of_work.execute(
            lambda _conn: self._repository.release(lease_session_id, lease_holder)
        )

    def holder(self, session_id: str) -> str | None:
        lease_session_id = str(session_id or "").strip()
        if not lease_session_id:
            return None
        now = float(self._clock())
        return self._unit_of_work.execute(
            lambda _conn: self._repository.holder(lease_session_id, now=now)
        )


__all__ = ["CompressionLeaseService"]
