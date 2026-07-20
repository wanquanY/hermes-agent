"""Wire-level identity rules for live and durable runtime events."""

from __future__ import annotations

import threading
import time
from typing import Any


TRANSIENT_PLATFORM_EVENT_TYPES = frozenset(
    {
        "agent.terminal.output",
        "terminal.close",
        "terminal.read.request",
    }
)


def is_transient_platform_event(frame: dict[str, Any]) -> bool:
    """Return whether a frame belongs to a renderer-owned side channel.

    Platform frames carry session/run identity for routing, but they are not
    conversation facts and therefore never enter the durable timeline cursor
    namespace.
    """
    return str(frame.get("type") or "").strip() in TRANSIENT_PLATFORM_EVENT_TYPES


class RuntimeSourceSequencer:
    """Allocate causal source order without touching the durable event ledger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seq = 0

    def claim(self, candidate: int = 0) -> int:
        with self._lock:
            if candidate > 0:
                self._last_seq = max(self._last_seq, candidate)
                return candidate
            next_seq = max(self._last_seq + 1, time.time_ns() // 1_000)
            self._last_seq = next_seq
            return next_seq

    def reset(self) -> None:
        with self._lock:
            self._last_seq = 0


_source_sequencer = RuntimeSourceSequencer()


def stamp_runtime_source_seq(
    frame: dict[str, Any],
    params: dict[str, Any],
    *,
    assign_if_missing: bool,
) -> int:
    """Stamp causal identity while leaving canonical seq ownership to SQLite."""
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    canonical_input = frame.get("transient") is False
    source_seq = _positive_int(
        frame.get("runtime_source_seq")
        or frame.get("runtimeSourceSeq")
        or payload.get("runtime_source_seq")
        or payload.get("runtimeSourceSeq")
        or (0 if canonical_input else frame.get("seq"))
        or (0 if canonical_input else payload.get("seq"))
    )
    if source_seq <= 0 and not assign_if_missing:
        return 0
    source_seq = _source_sequencer.claim(source_seq)
    frame["runtime_source_seq"] = source_seq
    payload["runtime_source_seq"] = source_seq
    frame["payload"] = payload
    params["runtime_source_seq"] = source_seq
    params_payload = params.get("payload")
    if isinstance(params_payload, dict):
        params_payload["runtime_source_seq"] = source_seq
    return source_seq


def mark_transient(
    frame: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> None:
    """Place a frame outside every durable cursor namespace."""
    candidates = (frame,) if params is None or params is frame else (frame, params)
    for candidate in candidates:
        candidate["transient"] = True
        candidate.pop("seq", None)
        payload = candidate.get("payload")
        if isinstance(payload, dict):
            payload.pop("seq", None)


def reset_for_tests() -> None:
    _source_sequencer.reset()


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


__all__ = [
    "RuntimeSourceSequencer",
    "TRANSIENT_PLATFORM_EVENT_TYPES",
    "is_transient_platform_event",
    "mark_transient",
    "reset_for_tests",
    "stamp_runtime_source_seq",
]
