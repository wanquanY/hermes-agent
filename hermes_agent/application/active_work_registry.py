"""Transport-neutral active-work lifecycle and bounded process drain."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

WorkCallback = Callable[[], Any | Awaitable[Any]]


class ActiveWorkState(str, Enum):
    ACCEPTING = "accepting"
    DRAINING = "draining"
    STOPPED = "stopped"


class WorkRejected(RuntimeError):
    """Raised when a new work item races with or follows process drain."""

    code = "runtime_draining"

    def __init__(self, *, work_id: str, kind: str, state: ActiveWorkState):
        self.work_id = work_id
        self.kind = kind
        self.state = state
        super().__init__(
            f"active work rejected: runtime is {state.value} "
            f"(kind={kind}, work_id={work_id})"
        )


@dataclass(frozen=True)
class ActiveWorkSnapshot:
    work_id: str
    kind: str
    surface: str
    generation: int
    started_at: float
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DrainReport:
    generation: int
    active_at_start: tuple[ActiveWorkSnapshot, ...]
    completed_in_grace: tuple[str, ...]
    deadline_expired: tuple[str, ...]
    timed_out: tuple[str, ...]
    callback_errors: tuple[str, ...]
    elapsed_seconds: float

    @property
    def clean(self) -> bool:
        return (
            not self.deadline_expired
            and not self.timed_out
            and not self.callback_errors
        )


@dataclass
class _ActiveWorkRecord:
    snapshot: ActiveWorkSnapshot
    persist_timeout: WorkCallback | None = None
    cancel: WorkCallback | None = None


class ActiveWorkLease:
    """Idempotent completion handle returned by :meth:`register`."""

    def __init__(self, registry: "ActiveWorkRegistry", work_id: str, generation: int):
        self._registry = registry
        self.work_id = work_id
        self.generation = generation
        self._released = False
        self._release_lock = threading.Lock()

    def set_callbacks(
        self,
        *,
        persist_timeout: WorkCallback | None = None,
        cancel: WorkCallback | None = None,
    ) -> bool:
        return self._registry._set_callbacks(
            self.work_id,
            self.generation,
            persist_timeout=persist_timeout,
            cancel=cancel,
        )

    def release(self) -> bool:
        with self._release_lock:
            if self._released:
                return False
            self._released = True
        return self._registry._release(self.work_id, self.generation)

    def __enter__(self) -> "ActiveWorkLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    async def __aenter__(self) -> "ActiveWorkLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class ActiveWorkRegistry:
    """Linearizes work admission, completion, and process drain.

    The registry owns lifecycle only. Domain state remains in the session,
    run, activity, or automation stores referenced by timeout callbacks.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._state = ActiveWorkState.ACCEPTING
        self._generation = 1
        self._records: dict[str, _ActiveWorkRecord] = {}

    @property
    def state(self) -> ActiveWorkState:
        with self._condition:
            return self._state

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    def start_accepting(self) -> int:
        """Open a new process generation after the prior one fully stopped."""

        with self._condition:
            if self._state is ActiveWorkState.ACCEPTING:
                return self._generation
            if self._records:
                raise RuntimeError(
                    "cannot start a new active-work generation while prior work remains"
                )
            self._generation += 1
            self._state = ActiveWorkState.ACCEPTING
            self._condition.notify_all()
            return self._generation

    def register(
        self,
        *,
        kind: str,
        surface: str,
        work_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        persist_timeout: WorkCallback | None = None,
        cancel: WorkCallback | None = None,
    ) -> ActiveWorkLease:
        stable_id = str(work_id or f"work_{uuid.uuid4().hex}")
        stable_kind = str(kind or "unknown")
        stable_surface = str(surface or "unknown")
        with self._condition:
            if self._state is not ActiveWorkState.ACCEPTING:
                raise WorkRejected(
                    work_id=stable_id,
                    kind=stable_kind,
                    state=self._state,
                )
            if stable_id in self._records:
                raise ValueError(f"active work id already registered: {stable_id}")
            snapshot = ActiveWorkSnapshot(
                work_id=stable_id,
                kind=stable_kind,
                surface=stable_surface,
                generation=self._generation,
                started_at=time.monotonic(),
                metadata=dict(metadata or {}),
            )
            self._records[stable_id] = _ActiveWorkRecord(
                snapshot=snapshot,
                persist_timeout=persist_timeout,
                cancel=cancel,
            )
            self._condition.notify_all()
            return ActiveWorkLease(self, stable_id, self._generation)

    def _set_callbacks(
        self,
        work_id: str,
        generation: int,
        *,
        persist_timeout: WorkCallback | None,
        cancel: WorkCallback | None,
    ) -> bool:
        with self._condition:
            record = self._records.get(work_id)
            if record is None or record.snapshot.generation != generation:
                return False
            record.persist_timeout = persist_timeout
            record.cancel = cancel
            return True

    def _release(self, work_id: str, generation: int) -> bool:
        with self._condition:
            record = self._records.get(work_id)
            if record is None or record.snapshot.generation != generation:
                return False
            del self._records[work_id]
            self._condition.notify_all()
            return True

    def snapshot(self) -> tuple[ActiveWorkSnapshot, ...]:
        with self._condition:
            return tuple(
                record.snapshot
                for record in sorted(
                    self._records.values(),
                    key=lambda item: (item.snapshot.started_at, item.snapshot.work_id),
                )
            )

    def begin_drain(self) -> tuple[ActiveWorkSnapshot, ...]:
        """Atomically reject future admissions and return current work."""

        with self._condition:
            if self._state is ActiveWorkState.ACCEPTING:
                self._state = ActiveWorkState.DRAINING
                self._condition.notify_all()
            return self.snapshot()

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._records:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self) -> None:
        with self._condition:
            if self._records:
                raise RuntimeError("cannot stop active-work registry while work remains")
            self._state = ActiveWorkState.STOPPED
            self._condition.notify_all()

    async def _invoke_callback(
        self,
        callback: WorkCallback | None,
        *,
        phase: str,
        work_id: str,
        errors: list[str],
    ) -> None:
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except BaseException as exc:
            message = f"{phase}:{work_id}:{type(exc).__name__}:{exc}"
            errors.append(message)
            logger.exception("active-work drain callback failed: %s", message)

    async def drain(
        self,
        *,
        timeout: float,
        cancel_grace: float = 5.0,
    ) -> DrainReport:
        """Drain current work, persisting timeout state before cancellation."""

        started = time.monotonic()
        active_at_start = self.begin_drain()
        if await asyncio.to_thread(self.wait_for_idle, timeout):
            self.stop()
            return DrainReport(
                generation=self.generation,
                active_at_start=active_at_start,
                completed_in_grace=tuple(item.work_id for item in active_at_start),
                deadline_expired=(),
                timed_out=(),
                callback_errors=(),
                elapsed_seconds=time.monotonic() - started,
            )

        with self._condition:
            timed_out_records = list(self._records.values())
        errors: list[str] = []
        for record in timed_out_records:
            await self._invoke_callback(
                record.persist_timeout,
                phase="persist",
                work_id=record.snapshot.work_id,
                errors=errors,
            )
        for record in timed_out_records:
            await self._invoke_callback(
                record.cancel,
                phase="cancel",
                work_id=record.snapshot.work_id,
                errors=errors,
            )

        await asyncio.to_thread(self.wait_for_idle, cancel_grace)
        remaining = self.snapshot()
        if not remaining:
            self.stop()
        completed_ids = {
            item.work_id for item in active_at_start
        } - {item.work_id for item in remaining}
        return DrainReport(
            generation=self.generation,
            active_at_start=active_at_start,
            completed_in_grace=tuple(sorted(completed_ids)),
            deadline_expired=tuple(
                record.snapshot.work_id for record in timed_out_records
            ),
            timed_out=tuple(item.work_id for item in remaining),
            callback_errors=tuple(errors),
            elapsed_seconds=time.monotonic() - started,
        )


process_active_work_registry = ActiveWorkRegistry()


def get_process_active_work_registry() -> ActiveWorkRegistry:
    return process_active_work_registry
