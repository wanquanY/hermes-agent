from __future__ import annotations

from collections.abc import Iterator

import tui_gateway._stdin_recovery as recovery_module
from tui_gateway._stdin_recovery import (
    StdinEofRecovery,
    StdinState,
    iter_stdin_lines,
)


class _ScriptedStream:
    def __init__(self, results: list[str | BaseException]) -> None:
        self._results: Iterator[str | BaseException] = iter(results)

    def readline(self) -> str:
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return result

    def fileno(self) -> int:
        return 0


def test_genuine_eof_is_not_retried(monkeypatch):
    monkeypatch.setattr(recovery_module, "_fcntl", object())
    monkeypatch.setattr(
        recovery_module,
        "inspect_stdin_state",
        lambda _fd: StdinState(nonblocking=False, receive_timeout=False),
    )
    logs: list[str] = []

    assert not StdinEofRecovery().handle_eof(logs.append)
    assert logs == ["stdin EOF (peer closed; O_NONBLOCK=clear, SO_RCVTIMEO=clear)"]


def test_nonblocking_eof_is_repaired_and_reading_resumes(monkeypatch):
    states = iter(
        [
            StdinState(nonblocking=True, receive_timeout=False),
            StdinState(nonblocking=False, receive_timeout=False),
        ]
    )
    restored: list[tuple[int, StdinState]] = []
    monkeypatch.setattr(recovery_module, "_fcntl", object())
    monkeypatch.setattr(
        recovery_module, "inspect_stdin_state", lambda _fd: next(states)
    )
    monkeypatch.setattr(
        recovery_module,
        "restore_stdin_state",
        lambda fd, state: (restored.append((fd, state)) or True, None),
    )
    stream = _ScriptedStream(["", "request\n", ""])
    logs: list[str] = []

    assert list(iter_stdin_lines(stream, log=logs.append)) == ["request\n"]
    assert restored[0][1].nonblocking is True
    assert "spurious EOF recovered" in logs[0]
    assert "peer closed" in logs[1]


def test_blocking_io_error_uses_the_same_recovery_path(monkeypatch):
    monkeypatch.setattr(recovery_module, "_fcntl", object())
    monkeypatch.setattr(
        recovery_module,
        "inspect_stdin_state",
        lambda _fd: StdinState(nonblocking=True, receive_timeout=False),
    )
    monkeypatch.setattr(
        recovery_module, "restore_stdin_state", lambda _fd, _state: (True, None)
    )
    stream = _ScriptedStream([BlockingIOError(), "request\n"])
    guard = StdinEofRecovery(max_recoveries=1)

    assert next(iter_stdin_lines(stream, log=lambda _message: None, recovery=guard)) == (
        "request\n"
    )


def test_receive_timeout_alone_is_treated_as_contamination(monkeypatch):
    state = StdinState(nonblocking=False, receive_timeout=True)
    restored: list[StdinState] = []
    monkeypatch.setattr(recovery_module, "_fcntl", object())
    monkeypatch.setattr(recovery_module, "inspect_stdin_state", lambda _fd: state)
    monkeypatch.setattr(
        recovery_module,
        "restore_stdin_state",
        lambda _fd, value: (restored.append(value) or True, None),
    )

    assert StdinEofRecovery().handle_eof(lambda _message: None)
    assert restored == [state]


def test_recovery_rate_is_capped(monkeypatch):
    state = StdinState(nonblocking=True, receive_timeout=False)
    monkeypatch.setattr(recovery_module, "_fcntl", object())
    monkeypatch.setattr(recovery_module, "inspect_stdin_state", lambda _fd: state)
    monkeypatch.setattr(
        recovery_module, "restore_stdin_state", lambda _fd, _state: (True, None)
    )
    times = iter([0.0, 1.0, 2.0])
    guard = StdinEofRecovery(max_recoveries=2, clock=lambda: next(times))
    logs: list[str] = []

    assert guard.handle_eof(logs.append)
    assert guard.handle_eof(logs.append)
    assert not guard.handle_eof(logs.append)
    assert "rate exceeded" in logs[-1]


def test_windows_path_treats_empty_read_as_real_eof(monkeypatch):
    monkeypatch.setattr(recovery_module, "_fcntl", None)
    logs: list[str] = []

    assert not StdinEofRecovery().handle_eof(logs.append)
    assert logs == ["stdin EOF (peer closed)"]


def test_failed_restore_stops_instead_of_busy_looping(monkeypatch):
    monkeypatch.setattr(recovery_module, "_fcntl", object())
    monkeypatch.setattr(
        recovery_module,
        "inspect_stdin_state",
        lambda _fd: StdinState(nonblocking=True, receive_timeout=True),
    )
    monkeypatch.setattr(
        recovery_module,
        "restore_stdin_state",
        lambda _fd, _state: (False, "permission denied"),
    )
    logs: list[str] = []

    assert not StdinEofRecovery().handle_eof(logs.append)
    assert "recovery failed" in logs[-1]
