"""Gateway interruption freshness policy."""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT = 60 * 60


def coerce_gateway_timestamp(value: Any) -> Optional[float]:
    """Best-effort conversion of stored gateway timestamps to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def auto_continue_freshness_window() -> float:
    """Return the configured auto-continue freshness window in seconds."""
    raw = os.environ.get("HERMES_AUTO_CONTINUE_FRESHNESS")
    if raw is None or raw == "":
        return float(AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)


def is_fresh_gateway_interruption(
    value: Any,
    *,
    now: Optional[float] = None,
    window_secs: Optional[float] = None,
) -> bool:
    """Return True when an interruption marker is fresh enough to auto-continue."""
    window = (
        float(window_secs)
        if window_secs is not None
        else float(AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    )
    if window <= 0:
        return True
    timestamp = coerce_gateway_timestamp(value)
    if timestamp is None:
        return True
    current = time.time() if now is None else now
    return current - timestamp <= window


def last_transcript_timestamp(history: Optional[List[Dict[str, Any]]]) -> Any:
    """Return the timestamp of the last usable transcript row, if any."""
    if not history:
        return None
    for msg in reversed(history):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if not role or role in {"session_meta", "system"}:
            continue
        ts = msg.get("timestamp")
        if ts is not None:
            return ts
        return None
    return None
