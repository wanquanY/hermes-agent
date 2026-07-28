"""Thread-local stdout/stderr silencing for background workers."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from typing import Iterator, TextIO

__all__ = ["thread_scoped_silence"]

_install_lock = threading.Lock()
_installed: dict[str, "_ThreadRoutingStream"] = {}


class _ThreadRoutingStream:
    def __init__(self, passthrough: TextIO, sink: TextIO) -> None:
        self._passthrough = passthrough
        self._sink = sink
        self._silenced: dict[int, int] = {}
        self._lock = threading.Lock()

    def _target(self) -> TextIO:
        return (
            self._sink
            if self._silenced.get(threading.get_ident(), 0) > 0
            else self._passthrough
        )

    def silence(self, ident: int) -> None:
        with self._lock:
            self._silenced[ident] = self._silenced.get(ident, 0) + 1

    def unsilence(self, ident: int) -> None:
        with self._lock:
            depth = self._silenced.get(ident, 0) - 1
            if depth > 0:
                self._silenced[ident] = depth
            else:
                self._silenced.pop(ident, None)

    def write(self, data):  # type: ignore[no-untyped-def]
        try:
            return self._target().write(data)
        except Exception:
            return len(data) if isinstance(data, str) else 0

    def flush(self):  # type: ignore[no-untyped-def]
        try:
            return self._target().flush()
        except Exception:
            return None

    def writelines(self, lines):  # type: ignore[no-untyped-def]
        try:
            return self._target().writelines(lines)
        except Exception:
            return None

    def isatty(self) -> bool:
        try:
            return bool(self._target().isatty())
        except Exception:
            return False

    def fileno(self):  # type: ignore[no-untyped-def]
        return self._target().fileno()

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._target(), name)


def _ensure_installed(attr: str, sink: TextIO) -> _ThreadRoutingStream:
    with _install_lock:
        current = getattr(sys, attr, None)
        installed = _installed.get(attr)
        if installed is not None and current is installed:
            return installed
        proxy = _ThreadRoutingStream(current if current is not None else sink, sink)
        setattr(sys, attr, proxy)
        _installed[attr] = proxy
        return proxy


@contextlib.contextmanager
def thread_scoped_silence() -> Iterator[None]:
    """Silence stdout/stderr for the calling thread and no other thread."""
    sink = open(os.devnull, "w", encoding="utf-8")
    ident = threading.get_ident()
    stdout = _ensure_installed("stdout", sink)
    stderr = _ensure_installed("stderr", sink)
    stdout.silence(ident)
    stderr.silence(ident)
    try:
        yield
    finally:
        stdout.unsilence(ident)
        stderr.unsilence(ident)
        sink.close()
