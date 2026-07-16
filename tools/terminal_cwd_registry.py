"""Session-scoped working directories for shared terminal environments.

Hermes deliberately lets multiple conversations share one long-lived terminal
environment.  The environment object's ``cwd`` is therefore only the cwd of the
last command that happened to finish, not a session-scoped source of truth.
This registry keeps the logical cwd per ``(environment, session)`` and provides
one execution lock per environment so command completion and cwd capture are
linearizable.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


def resolve_terminal_session_key(task_id: str | None = None) -> str:
    """Return the stable conversation key used for terminal cwd ownership."""

    try:
        from tools.approval import get_current_session_key

        current = str(get_current_session_key(default="") or "").strip()
    except Exception:
        current = ""
    return current or str(task_id or "").strip() or "default"


class TerminalCwdRegistry:
    """Thread-safe logical cwd storage for shared terminal environments."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cwds: dict[tuple[str, str], str] = {}
        self._execution_locks: dict[str, threading.RLock] = {}

    def get(self, environment_key: str, session_key: str) -> str | None:
        with self._lock:
            return self._cwds.get((environment_key, session_key))

    def resolve(
        self,
        *,
        environment_key: str,
        session_key: str,
        default_cwd: str,
        explicit_workdir: str | None = None,
    ) -> str:
        """Resolve explicit workdir, then session cwd, then configured root."""

        if explicit_workdir:
            return explicit_workdir
        return self.get(environment_key, session_key) or default_cwd

    def record(self, environment_key: str, session_key: str, cwd: object) -> None:
        normalized = str(cwd or "").strip()
        if not normalized:
            return
        with self._lock:
            self._cwds[(environment_key, session_key)] = normalized

    def discard_environment(self, environment_key: str) -> None:
        """Forget cwd state only after the corresponding environment is gone."""

        with self._lock:
            self._cwds = {
                key: value
                for key, value in self._cwds.items()
                if key[0] != environment_key
            }
            self._execution_locks.pop(environment_key, None)

    @contextmanager
    def execution_guard(self, environment_key: str) -> Iterator[None]:
        """Serialize shell-state mutation and cwd capture for one environment."""

        with self._lock:
            guard = self._execution_locks.setdefault(
                environment_key,
                threading.RLock(),
            )
        with guard:
            yield


terminal_cwd_registry = TerminalCwdRegistry()


__all__ = [
    "TerminalCwdRegistry",
    "resolve_terminal_session_key",
    "terminal_cwd_registry",
]
