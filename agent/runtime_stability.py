"""Agent-facing bridge to persisted runtime stability policy."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Mapping
from typing import Any


logger = logging.getLogger(__name__)


class StreamStaleCircuitOpen(RuntimeError):
    """Raised before I/O while a session/provider stale circuit is open."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def stream_stale_threshold() -> int:
    return max(0, _env_int("HERMES_STREAM_STALE_GIVEUP", 5))


def stream_stale_open_seconds() -> float:
    return max(1.0, _env_float("HERMES_STREAM_STALE_BREAKER_SECONDS", 900.0))


def stream_route_hash(agent: Any) -> str:
    material = "\x1f".join(
        (
            str(getattr(agent, "provider", "") or "").strip().lower(),
            str(getattr(agent, "base_url", "") or "").strip().lower(),
            str(getattr(agent, "model", "") or "").strip().lower(),
            str(getattr(agent, "api_mode", "") or "").strip().lower(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _service_and_session(agent: Any):
    session_id = str(getattr(agent, "session_id", "") or "").strip()
    session_db = getattr(agent, "_session_db", None)
    return getattr(session_db, "runtime_stability", None), session_id


def _state_value(state: Any, name: str, default: Any) -> Any:
    """Read direct domain snapshots and JSON-safe worker RPC projections."""
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def _apply_memory_state(agent: Any, state: Any) -> None:
    agent._stream_stale_failures = int(
        _state_value(state, "stream_stale_failures", 0) or 0
    )
    agent._stream_stale_retry_after = float(
        _state_value(state, "stream_stale_retry_after", 0) or 0
    )
    agent._stream_stale_route_hash = str(
        _state_value(state, "stream_stale_route_hash", "") or ""
    )


def reset_stream_stale_circuit(agent: Any, *, reason: str = "explicit") -> None:
    """Clear the current route's breaker in memory and SQLite."""
    agent._stream_stale_failures = 0
    agent._stream_stale_retry_after = 0.0
    agent._stream_stale_route_hash = ""
    service, session_id = _service_and_session(agent)
    if service is None or not session_id:
        return
    try:
        service.clear_stream_stale(session_id)
    except Exception:
        logger.warning(
            "stream stale reset persistence failed: session=%s reason=%s",
            session_id,
            reason,
            exc_info=True,
        )


def check_stream_stale_circuit(agent: Any) -> None:
    """Fail fast while open; expiry permits one half-open network attempt."""
    threshold = stream_stale_threshold()
    if threshold <= 0:
        return
    route_hash = stream_route_hash(agent)
    service, session_id = _service_and_session(agent)
    if service is not None and session_id:
        try:
            state = service.get(session_id)
            _apply_memory_state(agent, state)
        except Exception:
            logger.warning(
                "stream stale state restore failed: session=%s",
                session_id,
                exc_info=True,
            )

    stored_route = str(getattr(agent, "_stream_stale_route_hash", "") or "")
    if stored_route and stored_route != route_hash:
        reset_stream_stale_circuit(agent, reason="route_changed")
        return

    failures = int(getattr(agent, "_stream_stale_failures", 0) or 0)
    if failures < threshold:
        return
    retry_after = float(getattr(agent, "_stream_stale_retry_after", 0) or 0)
    now = time.time()
    if retry_after <= now:
        # Policy expiry is the half-open transition. Clear the old streak so
        # the next stale failure starts a fresh window; a success also clears.
        reset_stream_stale_circuit(agent, reason="half_open_expired")
        return
    remaining = max(1, int(retry_after - now))
    raise StreamStaleCircuitOpen(
        "Provider remained unresponsive across "
        f"{failures} stale attempts; this session route is paused for "
        f"another {remaining}s to avoid repeating full timeout/retry cycles. "
        "Use /retry to force one attempt now, switch models, or start a new session."
    )


def record_stream_stale_failure(agent: Any, error: str) -> int:
    """Atomically add one stale failure for the active session/route."""
    threshold = stream_stale_threshold()
    if threshold <= 0:
        return 0
    route_hash = stream_route_hash(agent)
    service, session_id = _service_and_session(agent)
    if service is not None and session_id:
        try:
            state = service.record_stream_stale_failure(
                session_id,
                route_hash=route_hash,
                threshold=threshold,
                open_seconds=stream_stale_open_seconds(),
                error=str(error or "stale provider response"),
            )
            _apply_memory_state(agent, state)
            return int(_state_value(state, "stream_stale_failures", 0) or 0)
        except Exception:
            logger.warning(
                "stream stale failure persistence failed: session=%s",
                session_id,
                exc_info=True,
            )

    previous_route = str(getattr(agent, "_stream_stale_route_hash", "") or "")
    failures = (
        int(getattr(agent, "_stream_stale_failures", 0) or 0) + 1
        if previous_route == route_hash
        else 1
    )
    agent._stream_stale_failures = failures
    agent._stream_stale_route_hash = route_hash
    agent._stream_stale_retry_after = (
        time.time() + stream_stale_open_seconds()
        if failures >= threshold
        else 0.0
    )
    return failures


def record_stream_success(agent: Any) -> None:
    if int(getattr(agent, "_stream_stale_failures", 0) or 0) > 0:
        reset_stream_stale_circuit(agent, reason="provider_success")


__all__ = [
    "StreamStaleCircuitOpen",
    "check_stream_stale_circuit",
    "record_stream_stale_failure",
    "record_stream_success",
    "reset_stream_stale_circuit",
    "stream_route_hash",
]
