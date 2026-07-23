"""Process-safe logging transport for Dovie run workers.

Run workers must never open the profile's rotating log files directly.  A
worker can coexist with the main sidecar and many sibling workers, while the
stdlib :class:`logging.handlers.RotatingFileHandler` is not safe when several
processes rotate the same path.

This module installs a root handler that snapshots ``LogRecord`` objects into
a bounded, thread-safe queue.  One asyncio task drains that queue into the
worker stdout protocol; the main sidecar remains the only process that
formats, redacts, and rotates files.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import traceback
from dataclasses import dataclass
from typing import Awaitable, Callable, Final


DEFAULT_WORKER_LOG_QUEUE_SIZE: Final[int] = 4096
_STOP: Final[object] = object()


@dataclass(frozen=True)
class WorkerLogEnvelope:
    """Serializable worker-side snapshot of one ``LogRecord``."""

    level: str
    text: str
    logger: str = ""
    created: float = 0.0
    process_id: int = 0
    thread_name: str = ""
    session_tag: str = ""
    pathname: str = ""
    line_no: int = 0
    exception: str = ""


def _exception_text(record: logging.LogRecord) -> str:
    parts: list[str] = []
    if record.exc_info:
        parts.append("".join(traceback.format_exception(*record.exc_info)).rstrip())
    elif record.exc_text:
        parts.append(str(record.exc_text).rstrip())
    if record.stack_info:
        parts.append(str(record.stack_info).rstrip())
    return "\n".join(part for part in parts if part)


def _snapshot_record(record: logging.LogRecord) -> WorkerLogEnvelope:
    try:
        text = record.getMessage()
    except Exception:
        text = str(record.msg)
    return WorkerLogEnvelope(
        level=record.levelname.lower(),
        text=text,
        logger=record.name,
        created=float(record.created),
        process_id=int(record.process or 0),
        thread_name=str(record.threadName or ""),
        session_tag=str(getattr(record, "session_tag", "") or ""),
        pathname=str(record.pathname or ""),
        line_no=int(record.lineno or 0),
        exception=_exception_text(record),
    )


class WorkerLogForwardingHandler(logging.Handler):
    """Non-file handler that hands records to ``WorkerLogBridge``."""

    _hermes_worker_forwarder = True

    def __init__(
        self,
        target: "queue.Queue[WorkerLogEnvelope | object]",
        on_low_priority_drop: Callable[[], None],
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._target = target
        self._on_low_priority_drop = on_low_priority_drop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            envelope = _snapshot_record(record)
            self._target.put_nowait(envelope)
        except queue.Full:
            if record.levelno < logging.WARNING:
                self._on_low_priority_drop()
                return
            # WARNING/ERROR must never disappear silently.  Avoid blocking the
            # agent or event-loop thread; stderr is inherited by the sidecar
            # and remains the last-resort crash/overload channel.
            _write_fallback(envelope)
        except Exception:
            self.handleError(record)


def _write_fallback(envelope: WorkerLogEnvelope) -> None:
    try:
        stderr = sys.__stderr__ or sys.stderr
        if stderr is None:
            return
        suffix = f"\n{envelope.exception}" if envelope.exception else ""
        stderr.write(
            f"[run-worker-log-fallback] {envelope.level.upper()} "
            f"{envelope.logger}: {envelope.text}{suffix}\n"
        )
        stderr.flush()
    except Exception:
        pass


class WorkerLogBridge:
    """Own the worker root handler and asynchronously forward its records."""

    def __init__(
        self,
        emit: Callable[[WorkerLogEnvelope], Awaitable[None]],
        *,
        queue_size: int = DEFAULT_WORKER_LOG_QUEUE_SIZE,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self._emit = emit
        self._queue: "queue.Queue[WorkerLogEnvelope | object]" = queue.Queue(
            maxsize=queue_size
        )
        self._drop_lock = threading.Lock()
        self._dropped_low_priority = 0
        self._handler = WorkerLogForwardingHandler(
            self._queue,
            self._record_low_priority_drop,
        )
        self._root = logging.getLogger()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def handler(self) -> WorkerLogForwardingHandler:
        return self._handler

    def start(self) -> None:
        if self._task is not None:
            return
        if self._closed:
            raise RuntimeError("worker log bridge is already closed")

        # Remove only the emergency worker fallback installed by
        # hermes_logging when a worker is initialized without its protocol.
        for handler in list(self._root.handlers):
            if getattr(handler, "_hermes_worker_fallback", False):
                self._root.removeHandler(handler)
                handler.close()
        self._root.addHandler(self._handler)
        if self._root.level == logging.NOTSET or self._root.level > logging.INFO:
            self._root.setLevel(logging.INFO)
        self._task = asyncio.create_task(
            self._drain(),
            name="run-worker-log-forwarder",
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._root.removeHandler(self._handler)
        if self._task is None:
            self._handler.close()
            return

        # Queue insertion can block only while the live drain task makes room;
        # doing it off-loop prevents shutdown from deadlocking the forwarder.
        await asyncio.to_thread(self._queue.put, _STOP)
        await self._task
        self._handler.close()

    def _record_low_priority_drop(self) -> None:
        with self._drop_lock:
            self._dropped_low_priority += 1

    def _take_low_priority_drop_count(self) -> int:
        with self._drop_lock:
            count = self._dropped_low_priority
            self._dropped_low_priority = 0
            return count

    async def _emit_drop_summary(self) -> None:
        count = self._take_low_priority_drop_count()
        if count <= 0:
            return
        await self._emit(
            WorkerLogEnvelope(
                level="warning",
                logger=__name__,
                text=(
                    "worker log transport dropped "
                    f"{count} DEBUG/INFO record(s) because its queue was full"
                ),
            )
        )

    async def _drain(self) -> None:
        while True:
            item = await asyncio.to_thread(self._queue.get)
            try:
                await self._emit_drop_summary()
                if item is _STOP:
                    return
                assert isinstance(item, WorkerLogEnvelope)
                try:
                    await self._emit(item)
                except Exception:
                    _write_fallback(item)
            finally:
                self._queue.task_done()
