"""Resolve profile-scoped Hermes home for session-bound cosmetic RPCs."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@contextmanager
def session_home_scope(
    sessions: Mapping[str, Any],
    params: Mapping[str, Any],
) -> Iterator[None]:
    session = sessions.get(str(params.get("session_id") or ""))
    context = session.get("profile_context") if isinstance(session, dict) else None
    home = str((context or {}).get("hermes_home") or "").strip()
    token = set_hermes_home_override(home) if home else None
    try:
        yield
    finally:
        if token is not None:
            reset_hermes_home_override(token)


__all__ = ["session_home_scope"]
