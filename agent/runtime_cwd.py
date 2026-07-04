"""Runtime working-directory resolution shared by agent subsystems."""

from __future__ import annotations

import os
from pathlib import Path

_SESSION_CWD: str = ""


def set_session_cwd(cwd: str | os.PathLike[str] | None) -> None:
    global _SESSION_CWD
    _SESSION_CWD = str(cwd or "").strip()


def clear_session_cwd() -> None:
    set_session_cwd("")


def resolve_agent_cwd() -> Path:
    if _SESSION_CWD:
        return Path(_SESSION_CWD).expanduser()
    try:
        from gateway.session_context import get_session_env

        session_cwd = get_session_env("TERMINAL_CWD", "").strip()
        if session_cwd:
            return Path(session_cwd).expanduser()
    except Exception:
        pass
    env_cwd = os.getenv("TERMINAL_CWD", "").strip()
    if env_cwd:
        return Path(env_cwd).expanduser()
    return Path(os.getcwd())
