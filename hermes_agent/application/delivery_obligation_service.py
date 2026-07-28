"""Application service for durable final-response delivery obligations."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Optional

from hermes_agent.domain.delivery_obligation import (
    DeliveryObligation,
    DeliveryObligationState,
    RecoverableDelivery,
    compute_delivery_obligation_id,
)
from hermes_agent.repositories.delivery_obligation_repo import (
    DeliveryObligationRepository,
)
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class DeliveryObligationService:
    MAX_ATTEMPTS = 3
    STALE_AFTER_SECONDS = 24 * 60 * 60
    RETENTION_SECONDS = 7 * 24 * 60 * 60
    MAX_ROWS = 500

    def __init__(
        self,
        repository: DeliveryObligationRepository,
        unit_of_work: SqliteUnitOfWork,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work

    def record(
        self,
        *,
        session_key: str,
        inbound_message_id: str,
        platform: str,
        chat_id: str,
        thread_id: Optional[str],
        reply_to: Optional[str],
        metadata: Optional[Mapping[str, Any]],
        content: str,
        owner_pid: int,
        owner_started_at: Optional[int],
        now: Optional[float] = None,
    ) -> str:
        timestamp = float(now if now is not None else time.time())
        obligation_id = compute_delivery_obligation_id(
            session_key,
            inbound_message_id,
            content,
        )
        obligation = DeliveryObligation(
            obligation_id=obligation_id,
            session_key=session_key,
            platform=platform,
            chat_id=str(chat_id),
            thread_id=str(thread_id) if thread_id else None,
            reply_to=str(reply_to) if reply_to else None,
            metadata=dict(metadata or {}),
            content=content,
            state=DeliveryObligationState.PENDING,
            attempts=0,
            created_at=timestamp,
            updated_at=timestamp,
            owner_pid=owner_pid,
            owner_started_at=owner_started_at,
        )

        def _write(_conn) -> None:
            self._repository.record(obligation)
            self._repository.prune(
                now=timestamp,
                retention_seconds=self.RETENTION_SECONDS,
                max_rows=self.MAX_ROWS,
            )

        self._unit_of_work.execute(_write)
        return obligation_id

    def mark_attempting(self, obligation_id: str) -> bool:
        return self._mark(obligation_id, DeliveryObligationState.ATTEMPTING)

    def mark_delivered(self, obligation_id: str) -> bool:
        return self._mark(obligation_id, DeliveryObligationState.DELIVERED)

    def mark_failed(self, obligation_id: str, error: str = "") -> bool:
        return self._mark(
            obligation_id,
            DeliveryObligationState.FAILED,
            error=error,
        )

    def _mark(
        self,
        obligation_id: str,
        state: DeliveryObligationState,
        *,
        error: str = "",
    ) -> bool:
        return self._unit_of_work.execute(
            lambda _conn: self._repository.mark_state(
                obligation_id,
                state,
                now=time.time(),
                error=error,
            )
        )

    def claim_recoverable(
        self,
        *,
        owner_pid: int,
        owner_started_at: Optional[int],
        owner_alive: Callable[[Any, Any], bool],
        deliverable_platforms: Optional[set[str]] = None,
        now: Optional[float] = None,
    ) -> list[RecoverableDelivery]:
        timestamp = float(now if now is not None else time.time())
        return self._unit_of_work.execute(
            lambda _conn: self._repository.claim_recoverable(
                now=timestamp,
                owner_pid=owner_pid,
                owner_started_at=owner_started_at,
                owner_alive=owner_alive,
                max_attempts=self.MAX_ATTEMPTS,
                stale_after_seconds=self.STALE_AFTER_SECONDS,
                deliverable_platforms=deliverable_platforms,
            )
        )

    def list_recent(self, limit: int = 20) -> list[DeliveryObligation]:
        return self._unit_of_work.read(
            lambda _conn: self._repository.list_recent(limit)
        )


__all__ = ["DeliveryObligationService"]
