"""Cross-platform advisory process locks for local durable stores."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


class ProcessLockError(RuntimeError):
    """The lock primitive could not safely establish ownership."""


@dataclass(frozen=True)
class ProcessLockLease:
    path: Path
    acquired: bool


def _lock_posix(handle: BinaryIO, *, blocking: bool) -> bool:
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - POSIX always has fcntl
        raise ProcessLockError("fcntl is unavailable") from exc
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        return False
    except OSError as exc:
        raise ProcessLockError(f"cannot acquire process lock: {exc}") from exc
    return True


def _unlock_posix(handle: BinaryIO) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_windows(handle: BinaryIO, *, blocking: bool) -> bool:
    try:
        import msvcrt
    except ImportError as exc:  # pragma: no cover - Windows always has msvcrt
        raise ProcessLockError("msvcrt is unavailable") from exc
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    try:
        msvcrt.locking(handle.fileno(), mode, 1)
    except OSError as exc:
        if not blocking:
            return False
        raise ProcessLockError(f"cannot acquire process lock: {exc}") from exc
    return True


def _unlock_windows(handle: BinaryIO) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def exclusive_process_lock(
    path: Path,
    *,
    blocking: bool = True,
) -> Iterator[ProcessLockLease]:
    """Acquire an advisory exclusive lock, failing closed on primitive errors.

    Contention is a normal result for a non-blocking acquisition and yields an
    unacquired lease. Filesystem, platform, acquire, or release failures raise
    :class:`ProcessLockError`; callers must not continue with an unguarded write.
    """

    lock_path = Path(path)
    handle: BinaryIO | None = None
    acquired = False
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
        except OSError as exc:
            raise ProcessLockError(
                f"cannot open process lock {lock_path}: {exc}"
            ) from exc
        acquired = (
            _lock_windows(handle, blocking=blocking)
            if os.name == "nt"
            else _lock_posix(handle, blocking=blocking)
        )
        yield ProcessLockLease(path=lock_path, acquired=acquired)
    finally:
        if handle is not None:
            try:
                if acquired:
                    if os.name == "nt":
                        _unlock_windows(handle)
                    else:
                        _unlock_posix(handle)
            except OSError as exc:
                raise ProcessLockError(
                    f"cannot release process lock {lock_path}: {exc}"
                ) from exc
            finally:
                handle.close()
