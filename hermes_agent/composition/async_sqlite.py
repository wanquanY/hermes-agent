"""Single-owner asynchronous boundary for synchronous SQLite services.

Hermes repositories deliberately stay synchronous: transactions, row mapping,
and migrations remain easy to reason about and test. Async gateway and worker
code must cross this facade instead of executing those calls on the event loop.

One process-wide, single-thread executor provides three important invariants:

* event-loop responsiveness does not depend on disk latency;
* async SQLite work is ordered on one owner thread without an ``asyncio.Lock``
  being held while I/O runs;
* cancellation and timeout stop waiting, not the underlying transaction.

The last point is essential for SQLite. Python cannot safely cancel a running
thread in the middle of a commit. The operation is therefore shielded and left
to finish on its owner thread while the caller receives cancellation/timeout.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import threading
from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


class AsyncSQLiteBoundaryClosed(RuntimeError):
    """Raised when work is submitted after boundary shutdown begins."""


class AsyncSQLiteBoundary:
    """Serialize synchronous SQLite work on a dedicated owner thread."""

    def __init__(self, *, thread_name_prefix: str = "hermes-sqlite") -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=thread_name_prefix,
        )
        self._state_lock = threading.Lock()
        self._closing = False
        self._shutdown_complete: concurrent.futures.Future[None] = (
            concurrent.futures.Future()
        )

    async def run(
        self,
        operation: Callable[..., _T],
        /,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _T:
        """Run ``operation`` off-loop while preserving request context.

        ``timeout`` bounds only the caller's wait. A transaction that already
        started is allowed to complete and remains ordered before later work.
        """

        with self._state_lock:
            if self._closing:
                raise AsyncSQLiteBoundaryClosed("SQLite async boundary is shutting down")
            executor = self._executor

        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        call = functools.partial(operation, *args, **kwargs)
        future = loop.run_in_executor(executor, context.run, call)
        try:
            protected = asyncio.shield(future)
            if timeout is None:
                return await protected
            return await asyncio.wait_for(protected, timeout=max(0.0, float(timeout)))
        except (asyncio.CancelledError, TimeoutError):
            # The executor future is deliberately shielded: forcibly aborting
            # a running SQLite commit is unsafe. Observe any later exception so
            # an abandoned waiter never produces an unhandled-future warning.
            future.add_done_callback(_consume_future_result)
            raise

    async def shutdown(self) -> None:
        """Drain accepted work and stop the owner thread exactly once.

        Concurrent shutdown callers share the same completion barrier. No
        caller can observe shutdown complete while accepted work is still
        running on the owner thread.
        """

        with self._state_lock:
            if self._closing:
                executor = None
            else:
                self._closing = True
                executor = self._executor
            completion = self._shutdown_complete

        if executor is None:
            await asyncio.shield(asyncio.wrap_future(completion))
            return

        loop = asyncio.get_running_loop()
        shutdown_future = loop.run_in_executor(
            None,
            functools.partial(executor.shutdown, wait=True, cancel_futures=False),
        )

        def _finish_shutdown(future: asyncio.Future[None]) -> None:
            try:
                future.result()
            except BaseException as exc:
                if not completion.done():
                    completion.set_exception(exc)
            else:
                if not completion.done():
                    completion.set_result(None)

        shutdown_future.add_done_callback(_finish_shutdown)
        await asyncio.shield(asyncio.wrap_future(completion))


def _consume_future_result(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except (asyncio.CancelledError, Exception):
        return


_boundary_lock = threading.Lock()
_boundary: AsyncSQLiteBoundary | None = None


def async_sqlite_boundary() -> AsyncSQLiteBoundary:
    """Return the canonical process-wide async SQLite owner."""

    global _boundary
    with _boundary_lock:
        if _boundary is None:
            _boundary = AsyncSQLiteBoundary()
        return _boundary


async def run_sqlite_io(
    operation: Callable[..., _T],
    /,
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> _T:
    """Execute synchronous repository/storage work through the owner."""

    return await async_sqlite_boundary().run(
        operation,
        *args,
        timeout=timeout,
        **kwargs,
    )


async def shutdown_async_sqlite_boundary() -> None:
    """Drain and clear the canonical owner during sidecar shutdown."""

    global _boundary
    with _boundary_lock:
        boundary = _boundary
    if boundary is not None:
        await boundary.shutdown()
        with _boundary_lock:
            if _boundary is boundary:
                _boundary = None


__all__ = [
    "AsyncSQLiteBoundary",
    "AsyncSQLiteBoundaryClosed",
    "async_sqlite_boundary",
    "run_sqlite_io",
    "shutdown_async_sqlite_boundary",
]
