from __future__ import annotations

import inspect
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_ACTIVE_RUN_STATUSES = (
    "queued",
    "starting",
    "running",
    "waiting_approval",
    "cancelling",
    "finalizing",
)
_DISABLE_ENV_KEYS = (
    "HERMES_STORAGE_MAINTENANCE_DISABLED",
    "DOVIE_STORAGE_MAINTENANCE_DISABLED",
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def storage_maintenance_disabled() -> bool:
    return any(_truthy(os.environ.get(key)) for key in _DISABLE_ENV_KEYS)


def _db_path(db: Any) -> Path | None:
    raw = getattr(db, "db_path", None)
    if raw is None:
        return None
    try:
        return Path(raw).expanduser()
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return default


def _sqlite_free_page_ratio(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"free_pages": 0, "page_count": 0, "free_ratio": 0.0}
    try:
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            free_pages = _safe_int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            page_count = _safe_int(conn.execute("PRAGMA page_count").fetchone()[0])
    except Exception:
        return {"free_pages": 0, "page_count": 0, "free_ratio": 0.0}
    ratio = (free_pages / page_count) if page_count > 0 else 0.0
    return {"free_pages": free_pages, "page_count": page_count, "free_ratio": ratio}


def _method_accepts_keyword(method: Any, keyword: str) -> bool:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == keyword:
            return True
    return False


@dataclass(frozen=True)
class MaintenanceTarget:
    key: str
    db: Any
    profile_home: str = ""


@dataclass
class MaintenanceTaskRecord:
    task: str
    status: str
    duration_s: float
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status,
            "duration_s": self.duration_s,
            "result": dict(self.result),
            "error": self.error,
        }


class StorageMaintenanceService:
    """Central owner for runtime storage maintenance scheduling.

    Phase 1 intentionally reuses existing DB methods instead of changing
    retention semantics. Later phases can swap the internals behind this
    service without adding more scattered startup/append hooks.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        interval_seconds: float = 60.0,
        terminal_stream_interval_seconds: float = 60.0,
        state_snapshot_interval_seconds: float = 300.0,
        retention_interval_seconds: float = 3600.0,
        vacuum_interval_seconds: float = 86400.0,
        vacuum_free_ratio_threshold: float = 0.20,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._interval_seconds = max(1.0, float(interval_seconds or 60.0))
        self._task_intervals = {
            "compact_run_events": max(1.0, float(terminal_stream_interval_seconds or 60.0)),
            "backfill_run_event_frame_blobs": max(1.0, float(state_snapshot_interval_seconds or 300.0)),
            "reference_run_event_payloads": max(1.0, float(retention_interval_seconds or 3600.0)),
            "prune_duplicate_session_info_events": max(1.0, float(state_snapshot_interval_seconds or 300.0)),
            "prune_run_events": max(1.0, float(retention_interval_seconds or 3600.0)),
            "vacuum": max(1.0, float(vacuum_interval_seconds or 86400.0)),
        }
        self._vacuum_free_ratio_threshold = max(0.0, float(vacuum_free_ratio_threshold or 0.0))
        self._targets: dict[str, MaintenanceTarget] = {}
        self._last_task_runs: dict[tuple[str, str], float] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_results: list[dict[str, Any]] = []

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def register_db(self, db: Any, *, profile_home: str | Path = "") -> str:
        path = _db_path(db)
        key = str(profile_home or path or id(db))
        target = MaintenanceTarget(key=key, db=db, profile_home=str(profile_home or ""))
        with self._lock:
            self._targets[key] = target
        return key

    def unregister_db(self, key: str) -> None:
        with self._lock:
            self._targets.pop(str(key or ""), None)

    def start(self) -> None:
        if storage_maintenance_disabled():
            return
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="storage-maintenance",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout or 0.0)))

    def last_results(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._last_results]

    def run_startup_cycle(self, db: Any) -> dict[str, Any]:
        if storage_maintenance_disabled():
            return {"skipped": True, "reason": "disabled"}
        method = getattr(db, "maybe_auto_compact_run_events", None)
        if not callable(method):
            return {"skipped": True, "reason": "method_missing"}
        started = time.monotonic()
        compaction_result: dict[str, Any] = {}
        try:
            result = self._call_startup_compaction(method)
            compaction_result = result if isinstance(result, dict) else {"result": result}
            record = MaintenanceTaskRecord(
                task="startup_run_event_compaction",
                status="completed",
                duration_s=time.monotonic() - started,
                result=dict(compaction_result),
            )
            self._remember_record(record)
        except Exception as exc:
            record = MaintenanceTaskRecord(
                task="startup_run_event_compaction",
                status="failed",
                duration_s=time.monotonic() - started,
                error=str(exc),
            )
            self._remember_record(record)
            raise
        # ADR-0001 Phase 0: backfill activity_id on legacy run_events rows.
        # Runs at most once per state.db (state_meta marker), bounded per
        # cycle so it does not stall startup. Failure here never aborts
        # startup — the compaction result is still returned.
        try:
            backfill_started = time.monotonic()
            from tui_gateway.services.storage_backfill_activity_id import (
                backfill_run_events_activity_id,
            )

            backfill_result = backfill_run_events_activity_id(db)
            self._remember_record(
                MaintenanceTaskRecord(
                    task="startup_activity_id_backfill",
                    status="completed" if not backfill_result.get("error") else "failed",
                    duration_s=time.monotonic() - backfill_started,
                    result=dict(backfill_result) if isinstance(backfill_result, dict) else {},
                    error=str(backfill_result.get("error") or "") if isinstance(backfill_result, dict) else "",
                )
            )
            if isinstance(backfill_result, dict) and not backfill_result.get("skipped"):
                compaction_result["activity_id_backfill"] = backfill_result
        except Exception as exc:
            self._logger.debug("activity_id backfill startup pass failed: %s", exc)
        return dict(compaction_result)

    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        if storage_maintenance_disabled():
            return {"skipped": True, "reason": "disabled", "targets": []}
        with self._lock:
            targets = list(self._targets.values())
        results = [self._run_target(target, force=force) for target in targets]
        return {
            "skipped": False,
            "target_count": len(targets),
            "targets": results,
        }

    def _run_loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.run_once()
            except Exception as exc:
                self._logger.debug("storage maintenance cycle failed: %s", exc)

    def _run_target(self, target: MaintenanceTarget, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        records: list[MaintenanceTaskRecord] = []
        records.append(
            self._run_scheduled_db_task(
                target,
                "compact_run_events",
                now=now,
                force=force,
            )
        )
        records.append(
            self._run_scheduled_db_task(
                target,
                "prune_duplicate_session_info_events",
                now=now,
                force=force,
            )
        )
        records.append(
            self._run_scheduled_db_task(
                target,
                "backfill_run_event_frame_blobs",
                now=now,
                force=force,
            )
        )
        records.append(
            self._run_scheduled_db_task(
                target,
                "reference_run_event_payloads",
                now=now,
                force=force,
            )
        )
        records.append(
            self._run_scheduled_db_task(
                target,
                "prune_run_events",
                now=now,
                force=force,
            )
        )
        records.append(self._run_scheduled_vacuum(target, now=now, force=force))
        for record in records:
            self._remember_record(record)
        return {
            "key": target.key,
            "profile_home": target.profile_home,
            "tasks": [record.to_dict() for record in records],
        }

    def _run_scheduled_db_task(
        self,
        target: MaintenanceTarget,
        method_name: str,
        *,
        now: float,
        force: bool,
    ) -> MaintenanceTaskRecord:
        due = self._task_due(target.key, method_name, now=now, force=force)
        if due is not None:
            return due
        record = self._call_db_task(target.db, method_name)
        self._mark_task_attempt(target.key, method_name, now, record)
        return record

    def _run_scheduled_vacuum(
        self,
        target: MaintenanceTarget,
        *,
        now: float,
        force: bool,
    ) -> MaintenanceTaskRecord:
        due = self._task_due(target.key, "vacuum", now=now, force=force)
        if due is not None:
            return due
        record = self._maybe_vacuum(target.db)
        self._mark_task_attempt(target.key, "vacuum", now, record)
        return record

    def _task_due(
        self,
        target_key: str,
        task: str,
        *,
        now: float,
        force: bool,
    ) -> MaintenanceTaskRecord | None:
        if force:
            return None
        interval = self._task_intervals.get(task)
        if interval is None:
            return None
        with self._lock:
            last_run = self._last_task_runs.get((target_key, task))
        if last_run is None:
            return None
        elapsed = now - last_run
        if elapsed >= interval:
            return None
        return MaintenanceTaskRecord(
            task=task,
            status="skipped",
            duration_s=0.0,
            result={
                "reason": "not_due",
                "next_due_seconds": max(0.0, interval - elapsed),
            },
        )

    def _mark_task_attempt(
        self,
        target_key: str,
        task: str,
        now: float,
        record: MaintenanceTaskRecord,
    ) -> None:
        if record.status == "failed":
            return
        with self._lock:
            self._last_task_runs[(target_key, task)] = now

    def _call_db_task(self, db: Any, method_name: str) -> MaintenanceTaskRecord:
        method = getattr(db, method_name, None)
        if not callable(method):
            return MaintenanceTaskRecord(
                task=method_name,
                status="skipped",
                duration_s=0.0,
                result={"reason": "method_missing"},
            )
        started = time.monotonic()
        try:
            result = method()
            if not isinstance(result, dict):
                result = {"result": result}
            return MaintenanceTaskRecord(
                task=method_name,
                status="completed",
                duration_s=time.monotonic() - started,
                result=dict(result),
            )
        except Exception as exc:
            self._logger.debug("storage maintenance task %s failed: %s", method_name, exc)
            return MaintenanceTaskRecord(
                task=method_name,
                status="failed",
                duration_s=time.monotonic() - started,
                error=str(exc),
            )

    def _maybe_vacuum(self, db: Any) -> MaintenanceTaskRecord:
        started = time.monotonic()
        vacuum = getattr(db, "vacuum", None)
        if not callable(vacuum):
            return MaintenanceTaskRecord(
                task="vacuum",
                status="skipped",
                duration_s=0.0,
                result={"reason": "method_missing"},
            )
        if self._has_active_runs(db):
            return MaintenanceTaskRecord(
                task="vacuum",
                status="skipped",
                duration_s=0.0,
                result={"reason": "active_runs"},
            )
        page_stats = _sqlite_free_page_ratio(_db_path(db))
        if _safe_float(page_stats.get("free_ratio")) < self._vacuum_free_ratio_threshold:
            return MaintenanceTaskRecord(
                task="vacuum",
                status="skipped",
                duration_s=0.0,
                result={"reason": "below_threshold", **page_stats},
            )
        try:
            vacuum()
            return MaintenanceTaskRecord(
                task="vacuum",
                status="completed",
                duration_s=time.monotonic() - started,
                result=page_stats,
            )
        except Exception as exc:
            self._logger.debug("storage maintenance vacuum failed: %s", exc)
            return MaintenanceTaskRecord(
                task="vacuum",
                status="failed",
                duration_s=time.monotonic() - started,
                result=page_stats,
                error=str(exc),
            )

    def _has_active_runs(self, db: Any) -> bool:
        method = getattr(db, "list_runs", None)
        if not callable(method):
            return False
        try:
            rows = method(statuses=list(_ACTIVE_RUN_STATUSES), limit=1)
        except TypeError:
            return False
        except Exception:
            return True
        return bool(rows)

    def _remember_record(self, record: MaintenanceTaskRecord) -> None:
        payload = record.to_dict()
        with self._lock:
            self._last_results.append(payload)
            self._last_results = self._last_results[-100:]

    def _call_startup_compaction(self, method: Any) -> Any:
        if _method_accepts_keyword(method, "vacuum"):
            return method(vacuum=False)
        return method()


_SERVICE: StorageMaintenanceService | None = None
_SERVICE_LOCK = threading.RLock()


def get_storage_maintenance_service(
    *,
    logger: logging.Logger | None = None,
) -> StorageMaintenanceService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = StorageMaintenanceService(logger=logger)
        return _SERVICE


def register_session_db_for_maintenance(
    db: Any,
    *,
    profile_home: str | Path = "",
    logger: logging.Logger | None = None,
    start: bool = True,
) -> StorageMaintenanceService:
    service = get_storage_maintenance_service(logger=logger)
    service.register_db(db, profile_home=profile_home)
    if start:
        service.start()
    return service


def run_startup_storage_maintenance(
    db: Any,
    *,
    profile_home: str | Path = "",
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    service = register_session_db_for_maintenance(
        db,
        profile_home=profile_home,
        logger=logger,
        start=True,
    )
    return service.run_startup_cycle(db)
