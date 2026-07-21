"""Recover TUI command pipes from subprocess-induced spurious EOF.

A child that inherits fd 0 can mutate the shared open-file description by
enabling ``O_NONBLOCK`` or ``SO_RCVTIMEO``.  CPython may surface the resulting
``EAGAIN`` as an empty ``TextIOWrapper.readline()``, which is otherwise
indistinguishable from a closed command pipe.

This module owns detection, repair, diagnostics, and loop rate limiting for
every process that reads the TUI JSON-lines protocol from stdin.  POSIX-only
imports are guarded so the TUI remains importable on Windows.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterator, TextIO

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]


MAX_RECOVERIES_PER_MINUTE = 10
_RECOVERY_WINDOW_SECONDS = 60.0
_SOCKET_TIMEOUT_BUFFER_SIZE = struct.calcsize("ll")


@dataclass(frozen=True)
class StdinState:
    """Observable mutations that can turn ``EAGAIN`` into apparent EOF."""

    nonblocking: bool | None
    receive_timeout: bool | None
    flag_error: str | None = None
    timeout_error: str | None = None

    @property
    def contaminated(self) -> bool:
        return self.nonblocking is True or self.receive_timeout is True

    def describe(self) -> str:
        parts = [_describe_state("O_NONBLOCK", self.nonblocking, self.flag_error)]
        parts.append(
            _describe_state("SO_RCVTIMEO", self.receive_timeout, self.timeout_error)
        )
        return ", ".join(parts)


def _describe_state(name: str, value: bool | None, error: str | None) -> str:
    if value is not None:
        return f"{name}={'set' if value else 'clear'}"
    if error:
        return f"{name}=unknown ({error})"
    return f"{name}=unavailable"


def _read_receive_timeout(fd: int) -> tuple[bool | None, str | None]:
    duplicate: socket.socket | None = None
    try:
        duplicate = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
        raw = duplicate.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_RCVTIMEO,
            _SOCKET_TIMEOUT_BUFFER_SIZE,
        )
        return any(raw), None
    except (AttributeError, OSError) as exc:
        return None, str(exc)
    finally:
        # socket.fromfd() duplicates the descriptor. Closing the wrapper
        # releases only that duplicate and never closes the original stdin.
        if duplicate is not None:
            duplicate.close()


def inspect_stdin_state(fd: int = 0) -> StdinState:
    """Inspect fd status without mutating it."""
    if _fcntl is None:
        return StdinState(nonblocking=None, receive_timeout=None)

    flag_error: str | None = None
    try:
        flags = _fcntl.fcntl(fd, _fcntl.F_GETFL)
        nonblocking: bool | None = bool(flags & os.O_NONBLOCK)
    except OSError as exc:
        nonblocking = None
        flag_error = str(exc)

    receive_timeout, timeout_error = _read_receive_timeout(fd)
    return StdinState(
        nonblocking=nonblocking,
        receive_timeout=receive_timeout,
        flag_error=flag_error,
        timeout_error=timeout_error,
    )


def _clear_receive_timeout(fd: int) -> None:
    duplicate = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        duplicate.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_RCVTIMEO,
            struct.pack("ll", 0, 0),
        )
    finally:
        duplicate.close()


def restore_stdin_state(fd: int, state: StdinState) -> tuple[bool, str | None]:
    """Restore blocking semantics for the mutations present in *state*."""
    errors: list[str] = []
    if state.nonblocking:
        try:
            os.set_blocking(fd, True)
        except OSError as exc:
            errors.append(f"O_NONBLOCK: {exc}")

    if state.receive_timeout:
        try:
            _clear_receive_timeout(fd)
        except (AttributeError, OSError) as exc:
            errors.append(f"SO_RCVTIMEO: {exc}")

    if errors:
        return False, "; ".join(errors)
    return True, None


class StdinEofRecovery:
    """Stateful, rate-limited recovery policy for one stdin reader."""

    def __init__(
        self,
        *,
        fd: int = 0,
        max_recoveries: int = MAX_RECOVERIES_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fd = fd
        self._max_recoveries = max_recoveries
        self._clock = clock
        self._recovery_times: deque[float] = deque()

    def handle_eof(self, log: Callable[[str], None]) -> bool:
        """Return whether the caller should retry after an apparent EOF."""
        if _fcntl is None:
            log("stdin EOF (peer closed)")
            return False

        state = inspect_stdin_state(self._fd)
        if not state.contaminated:
            log(f"stdin EOF (peer closed; {state.describe()})")
            return False

        now = self._clock()
        cutoff = now - _RECOVERY_WINDOW_SECONDS
        while self._recovery_times and self._recovery_times[0] <= cutoff:
            self._recovery_times.popleft()

        if len(self._recovery_times) >= self._max_recoveries:
            log(
                "stdin spurious-EOF recovery rate exceeded "
                f"({len(self._recovery_times) + 1}/min, cap {self._max_recoveries})"
            )
            return False
        self._recovery_times.append(now)

        restored, error = restore_stdin_state(self._fd, state)
        if not restored:
            log(
                "stdin spurious EOF detected but recovery failed: "
                f"{state.describe()}; {error}"
            )
            return False

        log(f"stdin spurious EOF recovered: {state.describe()}")
        return True


def iter_stdin_lines(
    stream: TextIO,
    *,
    log: Callable[[str], None],
    recovery: StdinEofRecovery | None = None,
) -> Iterator[str]:
    """Yield protocol lines, retrying when fd contamination fakes EOF."""
    guard = recovery or StdinEofRecovery(fd=stream.fileno())
    while True:
        try:
            raw = stream.readline()
        except BlockingIOError:
            raw = ""
        if raw:
            yield raw
            continue
        if not guard.handle_eof(log):
            return
