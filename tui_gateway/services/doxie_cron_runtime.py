from __future__ import annotations

import atexit
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any


_DEFAULT_INTERVAL_SECONDS = 15.0
_state_lock = threading.Lock()
_wake_event = threading.Event()
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_started_at: float | None = None
_last_tick_at: float | None = None
_last_success_at: float | None = None
_last_error_at: float | None = None
_last_error: str | None = None
_tick_count = 0


def _now_iso(timestamp: float | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _interval_seconds() -> float:
    raw = os.getenv("DOXIE_CRON_TICK_INTERVAL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _DEFAULT_INTERVAL_SECONDS


def _record_tick(*, success: bool, error: Exception | None = None) -> None:
    global _last_tick_at, _last_success_at, _last_error_at, _last_error, _tick_count
    now = time.time()
    with _state_lock:
        _last_tick_at = now
        _tick_count += 1
        if success:
            _last_success_at = now
            _last_error = None
        else:
            _last_error_at = now
            _last_error = str(error or "unknown cron tick error")


def _tick_once() -> None:
    from cron.scheduler import tick as cron_tick

    try:
        cron_tick(verbose=False)
    except Exception as exc:
        _record_tick(success=False, error=exc)
    else:
        _record_tick(success=True)


def _run_loop(interval_seconds: float) -> None:
    while not _stop_event.is_set():
        _wake_event.clear()
        _tick_once()
        _wake_event.wait(interval_seconds)


def start_cron_ticker(*, interval_seconds: float | None = None) -> None:
    """Start the Doxie sidecar-owned Hermes cron ticker.

    Doxie runs Hermes through the lightweight TUI JSON-RPC gateway rather than
    Hermes' full messaging gateway. The full gateway owns its own cron ticker;
    this sidecar must therefore own the scheduler loop for Doxie profile
    runtimes so cron.manage jobs actually execute.
    """
    global _thread, _started_at
    with _state_lock:
        if _thread and _thread.is_alive():
            return
        _stop_event.clear()
        _started_at = time.time()
        interval = interval_seconds if interval_seconds is not None else _interval_seconds()
        _thread = threading.Thread(
            target=_run_loop,
            args=(interval,),
            daemon=True,
            name="doxie-cron-ticker",
        )
        _thread.start()


def stop_cron_ticker(timeout: float = 5.0) -> None:
    with _state_lock:
        thread = _thread
    _stop_event.set()
    _wake_event.set()
    if thread and thread.is_alive():
        thread.join(timeout=timeout)


def request_cron_tick() -> None:
    """Wake the ticker so newly due jobs do not wait for the next interval."""
    _wake_event.set()


def cron_ticker_status() -> dict[str, Any]:
    with _state_lock:
        running = bool(_thread and _thread.is_alive())
        return {
            "source": "doxie-sidecar-ticker",
            "healthy": running and not _last_error,
            "running": running,
            "intervalSeconds": _interval_seconds(),
            "startedAt": _now_iso(_started_at),
            "lastTickAt": _now_iso(_last_tick_at),
            "lastSuccessAt": _now_iso(_last_success_at),
            "lastErrorAt": _now_iso(_last_error_at),
            "lastError": _last_error,
            "tickCount": _tick_count,
        }


atexit.register(stop_cron_ticker)
