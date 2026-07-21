"""Out-of-loop gateway shutdown backstop and loop heartbeat."""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S = 60.0
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
_HEARTBEAT_RELATIVE = ("state", "gateway.heartbeat")
_WATCHDOG_DUMP_RELATIVE = ("logs", "gateway-shutdown-watchdog.log")


def _process_hermes_home() -> Path:
    """Resolve process identity storage without profile-local overrides."""
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured) if configured else get_hermes_home()


def get_loop_heartbeat_path(home: Optional[Path] = None) -> Path:
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_HEARTBEAT_RELATIVE)


def get_shutdown_watchdog_dump_path(home: Optional[Path] = None) -> Path:
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_WATCHDOG_DUMP_RELATIVE)


def write_loop_heartbeat(
    *,
    pid: Optional[int] = None,
    start_time: Optional[float] = None,
    home: Optional[Path] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Path:
    """Atomically publish evidence that the asyncio loop is progressing."""
    path = get_loop_heartbeat_path(home)
    payload: dict[str, Any] = {
        "pid": int(pid if pid is not None else os.getpid()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "monotonic": time.monotonic(),
    }
    if start_time is not None:
        payload["start_time"] = float(start_time)
    if extra:
        payload.update(extra)
    try:
        atomic_json_write(path, payload, indent=None)
    except Exception:
        logger.debug("Failed to write gateway loop heartbeat", exc_info=True)
    return path


def resolve_shutdown_watchdog_delay(
    drain_timeout: float,
    *,
    grace_s: float = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
) -> float:
    try:
        drain = max(float(drain_timeout), 0.0)
    except (TypeError, ValueError):
        drain = 0.0
    try:
        grace = max(float(grace_s), 0.0)
    except (TypeError, ValueError):
        grace = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    return drain + grace


def _write_watchdog_dump(
    dump_path: Path,
    *,
    delay_s: float,
    snapshot: Optional[dict[str, Any]],
) -> None:
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    header = {
        "event": "shutdown_watchdog_fired",
        "pid": os.getpid(),
        "delay_s": delay_s,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot or {},
    }
    try:
        with open(dump_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(header, default=str) + "\n")
            handle.write("--- faulthandler dump (all threads) ---\n")
            handle.flush()
            try:
                faulthandler.dump_traceback(file=handle, all_threads=True)
            except Exception:
                handle.write("(faulthandler.dump_traceback failed)\n")
            handle.write("--- end dump ---\n")
            handle.flush()
    except Exception:
        logger.debug("Failed to write shutdown watchdog dump", exc_info=True)

    try:
        sys.stderr.write(
            f"Gateway shutdown watchdog fired after {delay_s:.0f}s "
            f"(pid={os.getpid()}); dumping all thread stacks.\n"
        )
        sys.stderr.flush()
        faulthandler.dump_traceback(all_threads=True)
    except Exception:
        logger.debug("Failed to emit shutdown watchdog dump to stderr", exc_info=True)


def arm_shutdown_watchdog(
    delay_s: float,
    *,
    done_event: Optional[threading.Event] = None,
    snapshot_fn: Optional[Callable[[], dict[str, Any]]] = None,
    exit_code: int = 1,
    dump_path: Optional[Path] = None,
    name: str = "gateway-shutdown-watchdog",
) -> threading.Event:
    """Hard-exit from a daemon thread if loop-owned shutdown wedges."""
    done = done_event if done_event is not None else threading.Event()
    try:
        delay = max(float(delay_s), 0.0)
    except (TypeError, ValueError):
        delay = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    if delay <= 0:
        return done

    def _watchdog() -> None:
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if done.wait(timeout=min(remaining, 1.0)):
                return
        if done.is_set():
            return

        snapshot: Optional[dict[str, Any]] = None
        if snapshot_fn is not None:
            try:
                snapshot = snapshot_fn()
            except Exception as exc:
                snapshot = {"snapshot_error": repr(exc)}
        target = dump_path or get_shutdown_watchdog_dump_path()
        _write_watchdog_dump(target, delay_s=delay, snapshot=snapshot)
        try:
            logger.critical(
                "Shutdown watchdog fired after %.0fs; forcing process exit "
                "because the asyncio drain appears wedged (see %s)",
                delay,
                target,
            )
        except Exception:
            _write_emergency_stderr("shutdown watchdog critical log failed")
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                logger.debug("Failed to flush stream during forced shutdown", exc_info=True)
        try:
            from channels.runtime_status import (
                release_gateway_runtime_lock,
                remove_pid_file,
            )

            remove_pid_file()
            release_gateway_runtime_lock()
        except Exception:
            logger.debug("Failed to release gateway runtime markers", exc_info=True)
        os._exit(exit_code)

    try:
        threading.Thread(target=_watchdog, daemon=True, name=name).start()
    except Exception:
        logger.debug("Failed to arm shutdown watchdog", exc_info=True)
    return done


def _write_emergency_stderr(message: str) -> bool:
    """Write without the logging stack when that stack itself has failed."""
    try:
        os.write(2, f"{message}\n".encode("utf-8", errors="replace"))
    except OSError:
        return False
    return True


async def loop_heartbeat_forever(
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    start_time: Optional[float] = None,
    home: Optional[Path] = None,
    should_continue: Optional[Callable[[], bool]] = None,
) -> None:
    """Refresh loop liveness until cancellation or the supplied gate closes."""
    try:
        interval = max(float(interval_s), 1.0)
    except (TypeError, ValueError):
        interval = DEFAULT_HEARTBEAT_INTERVAL_S
    write_loop_heartbeat(start_time=start_time, home=home)
    while True:
        if should_continue is not None and not should_continue():
            return
        await asyncio.sleep(interval)
        if should_continue is not None and not should_continue():
            return
        write_loop_heartbeat(start_time=start_time, home=home)


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S",
    "arm_shutdown_watchdog",
    "get_loop_heartbeat_path",
    "get_shutdown_watchdog_dump_path",
    "loop_heartbeat_forever",
    "resolve_shutdown_watchdog_delay",
    "write_loop_heartbeat",
]
