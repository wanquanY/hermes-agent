"""Session-owned turn serialization for Gateway transcript integrity.

Routing-key busy guards prevent two turns from the same chat lane from
overlapping, but a durable Hermes conversation is owned by ``session_id``.
Session navigation can map multiple routing keys to one conversation, so the
entire ``load transcript -> run agent -> persist transcript`` region also needs
serialization at the resolved conversation boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_LEASES = 512
DEFAULT_LEASE_WAIT_SECONDS = 1800.0


@dataclass(slots=True)
class TurnLeaseToken:
    """Identity-checked ownership token for one conversation turn."""

    session_id: str
    owner_key: str
    generation: int
    degraded: bool = False
    released: bool = False


class _SessionLease:
    __slots__ = ("lock", "holder", "acquired_at", "last_used")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.holder: Optional[TurnLeaseToken] = None
        self.acquired_at = 0.0
        self.last_used = time.time()

    @property
    def idle(self) -> bool:
        return self.holder is None and not self.lock.locked()


class SessionTurnLeaseRegistry:
    """Process-local leases keyed by the resolved durable ``session_id``."""

    def __init__(self, max_entries: int = DEFAULT_MAX_LEASES) -> None:
        self._leases: dict[str, _SessionLease] = {}
        self._max_entries = max(1, int(max_entries))

    def __len__(self) -> int:
        return len(self._leases)

    def _get_or_create(self, session_id: str) -> _SessionLease:
        lease = self._leases.get(session_id)
        if lease is None:
            self._evict_idle()
            lease = _SessionLease()
            self._leases[session_id] = lease
        lease.last_used = time.time()
        return lease

    def _evict_idle(self) -> None:
        overflow = len(self._leases) - self._max_entries + 1
        if overflow <= 0:
            return
        idle_ids = sorted(
            (session_id for session_id, lease in self._leases.items() if lease.idle),
            key=lambda session_id: self._leases[session_id].last_used,
        )
        for session_id in idle_ids[:overflow]:
            self._leases.pop(session_id, None)

    async def acquire(
        self,
        session_id: str,
        *,
        owner_key: str,
        generation: int,
        timeout: Optional[float] = None,
    ) -> Optional[TurnLeaseToken]:
        """Acquire a conversation lease, failing open after ``timeout``."""
        if not session_id:
            return None
        wait_seconds = (
            float(timeout)
            if timeout is not None and float(timeout) > 0
            else DEFAULT_LEASE_WAIT_SECONDS
        )
        token = TurnLeaseToken(session_id, owner_key, int(generation))
        lease = self._get_or_create(session_id)

        if lease.lock.locked():
            holder = lease.holder
            logger.warning(
                "turn lease contention on session %s: routing key %s (gen %s) "
                "waiting behind routing key %s (gen %s, held %.0fs); "
                "serializing both turns against the durable transcript",
                session_id,
                owner_key,
                generation,
                holder.owner_key if holder else "?",
                holder.generation if holder else "?",
                time.time() - lease.acquired_at if lease.acquired_at else -1.0,
            )

        try:
            await asyncio.wait_for(lease.lock.acquire(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            holder = lease.holder
            logger.error(
                "turn lease wait timed out after %.0fs on session %s "
                "(waiter %s gen %s; holder %s gen %s); failing open and "
                "running this turn unserialized",
                wait_seconds,
                session_id,
                owner_key,
                generation,
                holder.owner_key if holder else "?",
                holder.generation if holder else "?",
            )
            token.degraded = True
            return token

        lease.holder = token
        lease.acquired_at = time.time()
        lease.last_used = lease.acquired_at
        return token

    def rebind(self, token: Optional[TurnLeaseToken], new_session_id: str) -> bool:
        """Make a held lease follow an in-turn conversation-id rotation."""
        if (
            token is None
            or token.degraded
            or token.released
            or not new_session_id
            or new_session_id == token.session_id
        ):
            return False
        lease = self._leases.get(token.session_id)
        if lease is None or lease.holder is not token:
            return False

        existing = self._leases.get(new_session_id)
        if existing is not None and existing is not lease and not existing.idle:
            holder = existing.holder
            logger.warning(
                "turn lease rebind blocked: session %s rotated to %s while "
                "the target lease is live (current %s gen %s; target %s gen %s); "
                "keeping the original lease and failing open for the alias edge",
                token.session_id,
                new_session_id,
                token.owner_key,
                token.generation,
                holder.owner_key if holder else "?",
                holder.generation if holder else "?",
            )
            return False

        self._leases[new_session_id] = lease
        lease.last_used = time.time()
        token.session_id = new_session_id
        return True

    def release(self, token: Optional[TurnLeaseToken]) -> bool:
        """Release only the exact currently-held token; safe and idempotent."""
        if token is None or token.degraded or token.released:
            return False
        token.released = True
        lease = self._leases.get(token.session_id)
        if lease is None or lease.holder is not token:
            return False
        lease.holder = None
        lease.acquired_at = 0.0
        lease.last_used = time.time()
        if lease.lock.locked():
            lease.lock.release()
        return True


def _positive_timeout_from_env() -> float:
    for name in ("HERMES_TURN_LEASE_TIMEOUT", "HERMES_AGENT_TIMEOUT"):
        try:
            value = float(os.getenv(name, ""))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return DEFAULT_LEASE_WAIT_SECONDS


class GatewaySessionTurnLeaseService:
    """Runner-scoped ownership service for generation-bound lease tokens."""

    def __init__(
        self,
        runner,
        *,
        registry: Optional[SessionTurnLeaseRegistry] = None,
    ) -> None:
        self._runner = runner
        self._registry = registry or SessionTurnLeaseRegistry()
        self._tokens: dict[tuple[str, int], TurnLeaseToken] = {}

    async def acquire(
        self,
        session_id: str,
        *,
        routing_key: str,
        generation: int,
    ) -> Optional[TurnLeaseToken]:
        token = await self._registry.acquire(
            session_id,
            owner_key=routing_key,
            generation=generation,
            timeout=_positive_timeout_from_env(),
        )
        if token is not None:
            self._tokens[(routing_key, int(generation))] = token
        return token

    def release(self, routing_key: str, generation: int) -> bool:
        if not routing_key:
            return False
        token = self._tokens.pop((routing_key, int(generation)), None)
        return self._registry.release(token)

    def rebind(
        self,
        routing_key: str,
        generation: int,
        new_session_id: str,
    ) -> bool:
        token = self._tokens.get((routing_key, int(generation)))
        return self._registry.rebind(token, new_session_id)


def session_turn_lease_for(runner) -> GatewaySessionTurnLeaseService:
    """Return the runner's isolated lease owner, including for bare runners."""
    service = getattr(runner, "session_turn_leases", None)
    if isinstance(service, GatewaySessionTurnLeaseService):
        return service
    service = GatewaySessionTurnLeaseService(runner)
    runner.session_turn_leases = service
    return service
