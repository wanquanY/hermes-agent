"""Bounded, non-destructive probes for authenticated readiness surfaces."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

_DISK_DEGRADED_PERCENT = 90.0


def _check(status: str, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if detail:
        result["detail"] = detail
    result.update(extra)
    return result


def _probe_state_db(home: Path) -> dict[str, Any]:
    path = home / "state.db"
    if not path.exists():
        return _check("ok", "not initialized")
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return _check("ok")
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if not path.exists():
        return _check("ok", "using defaults")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            return _check("degraded", "top level is not a mapping")
        return _check("ok")
    except Exception as exc:
        return _check("degraded", f"invalid config ({type(exc).__name__})")


def _probe_disk(home: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(home)
        used_percent = (
            round((usage.used / usage.total) * 100, 1)
            if usage.total
            else 0.0
        )
        status = "degraded" if used_percent >= _DISK_DEGRADED_PERCENT else "ok"
        return _check(
            status,
            used_percent=used_percent,
            free_bytes=usage.free,
        )
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_gateway(runtime: dict[str, Any]) -> dict[str, Any]:
    state = str(runtime.get("gateway_state") or "unknown")
    platforms = runtime.get("platforms")
    connected = 0
    configured = 0
    if isinstance(platforms, dict):
        configured = len(platforms)
        connected = sum(
            1
            for value in platforms.values()
            if isinstance(value, dict)
            and str(value.get("state") or value.get("status") or "").lower()
            in {"connected", "running", "ok"}
        )
    status = "ok" if state in {"running", "draining"} else "degraded"
    return _check(
        status,
        state=state,
        connected_platforms=connected,
        platforms=configured,
    )


def collect_runtime_readiness(
    *,
    configured_model: str,
    runtime_status: dict[str, Any] | None,
    active_api_runs: int = 0,
    process_completion_queue_depth: int = 0,
    active_delegations: int = 0,
) -> dict[str, Any]:
    """Return counts and health states without exposing values or paths."""
    home = get_hermes_home()
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    checks = {
        "state_db": _probe_state_db(home),
        "config": _probe_config(home),
        "model": _check(
            "ok" if str(configured_model or "").strip() else "degraded"
        ),
        "disk": _probe_disk(home),
        "gateway": _probe_gateway(runtime),
        "background_queues": _check(
            "ok",
            active_api_runs=max(0, int(active_api_runs)),
            process_completions=max(0, int(process_completion_queue_depth)),
            active_delegations=max(0, int(active_delegations)),
        ),
    }
    overall = (
        "ok"
        if all(item.get("status") == "ok" for item in checks.values())
        else "degraded"
    )
    return {"status": overall, "checks": checks}


__all__ = ["collect_runtime_readiness"]
