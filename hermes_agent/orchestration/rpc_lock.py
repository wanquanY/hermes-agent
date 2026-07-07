"""ShardedRpcLock — per-session async lock with timeout + lease TTL (spec §8.2).

Legacy design used one global ``asyncio.Lock`` for every worker DB RPC — all
sessions serialize even when writing disjoint tables. This shard replaces
it with a per-session lock, so cross-session traffic runs concurrently and
same-session traffic still serializes (required to keep seq monotonic).

Guarantees:
* ``timeout`` — bounded wait; ``RpcBusy`` on expiry (spec §J11 recoverable)
* ``lease_ttl`` — watchdog kicks the owner off if it crashes with the lock
* long ops (compact, archive) request a longer timeout explicitly
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from hermes_agent.domain.exceptions import StorageBusyError


_logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT = 5.0
DEFAULT_LEASE_TTL = 30.0
LONG_OP_TIMEOUT = 30.0


class RpcBusy(StorageBusyError):
    """Raised when a session-scoped RPC lock could not be acquired in time.

    Callers must classify as recoverable — spec §8.2 asks them to log
    ``event=degrade cause=rpc_busy`` and either back off + retry or abort
    the run via ``terminate_run(cause=RPC_BUSY_ABORT)``.
    """


@dataclass
class LockLease:
    """Handle returned by ``acquire``. Release via ``release()`` or ``with`` block."""

    session_id: str
    granted_at: float
    lease_ttl: float
    _lock: "asyncio.Lock"
    _released: bool = field(default=False, init=False)

    async def __aenter__(self) -> "LockLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._lock.release()
        except RuntimeError:
            _logger.warning(
                "ShardedRpcLock double-release for session %s", self.session_id
            )


class ShardedRpcLock:
    """Session-keyed async lock pool.

    Sharding by session_id keeps the invariant that same-session events are
    ordered (SeqAllocator counts on it) while allowing cross-session
    parallelism.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_leases: dict[str, LockLease] = {}
        self._make_lock_lock: Optional[asyncio.Lock] = None

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def acquire(
        self,
        session_id: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> LockLease:
        """Acquire the per-session lock, honoring ``timeout``. Raise ``RpcBusy``
        if the wait exceeds ``timeout``.
        """
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            raise ValueError("session_id is required for ShardedRpcLock.acquire")
        lock = self._lock_for(stable_sid)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RpcBusy(
                f"ShardedRpcLock busy for session {stable_sid} "
                f"(timeout={timeout}s)"
            ) from exc

        granted_at = time.monotonic()
        lease = LockLease(
            session_id=stable_sid,
            granted_at=granted_at,
            lease_ttl=lease_ttl,
            _lock=lock,
        )
        self._active_leases[stable_sid] = lease
        return lease

    def peek_lease(self, session_id: str) -> LockLease | None:
        return self._active_leases.get(str(session_id or "").strip())

    def active_session_ids(self) -> list[str]:
        return sorted(self._active_leases.keys())

    def size(self) -> int:
        return len(self._locks)
