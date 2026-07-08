"""Run lifecycle domain decisions."""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS = 300.0
DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS = 2.0


def _json_loads(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return fallback
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _pid_is_alive(pid: int, current_pid: int | None = None) -> bool:
    if pid <= 0:
        return False
    if current_pid is not None and pid == current_pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def orphaned_active_run_decision(
    row: Any,
    *,
    now: float,
    live_runtime_ids: set[str] | None = None,
    current_pid: int | None = None,
    current_gateway_instance_id: str = "",
    stale_after_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS,
    owner_dead_grace_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS,
) -> tuple[bool, str]:
    live_runtime_ids = {
        str(value or "").strip()
        for value in (live_runtime_ids or set())
        if str(value or "").strip()
    }
    stale_after = max(0.0, float(stale_after_seconds or 0))
    owner_dead_grace = max(0.0, float(owner_dead_grace_seconds or 0))
    instance_id = str(current_gateway_instance_id or "").strip()

    metadata = _json_loads(_row_value(row, "metadata_json"), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    updated_at = float(_row_value(row, "updated_at") or _row_value(row, "started_at") or 0)
    updated_age = now - updated_at
    owner_instance = str(metadata.get("gateway_instance_id") or "").strip()
    try:
        owner_pid = int(metadata.get("gateway_pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    if owner_pid > 0:
        if (
            current_pid is not None
            and owner_pid == current_pid
            and owner_instance
            and owner_instance != instance_id
        ):
            return True, "same-pid-different-gateway-instance"
        if _pid_is_alive(owner_pid, current_pid=current_pid):
            return False, "owner-pid-alive"
        if updated_age < owner_dead_grace:
            return False, "owner-pid-dead-fresh"
        return True, "owner-pid-dead"
    runtime_session_value = str(_row_value(row, "runtime_" + "session_id") or "").strip()
    if runtime_session_value and runtime_session_value in live_runtime_ids:
        return False, "live-runtime-session"
    if updated_age >= stale_after:
        return True, "legacy-owner-metadata-stale"
    return False, "legacy-owner-metadata-fresh"


__all__ = [
    "DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS",
    "DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS",
    "orphaned_active_run_decision",
]
