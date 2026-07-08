"""State database maintenance orchestration."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class StateMaintenanceMixin:
    # ── Space reclamation ──

    def vacuum(self) -> None:
        """Run VACUUM to reclaim disk space after large deletes.

        SQLite does not shrink the database file when rows are deleted —
        freed pages just get reused on the next insert. After a prune that
        removed hundreds of sessions, the file stays bloated unless we
        explicitly VACUUM.

        VACUUM rewrites the entire DB, so it's expensive (seconds per
        100MB) and cannot run inside a transaction. It also acquires an
        exclusive lock, so callers must ensure no other writers are
        active. Safe to call at startup before the gateway/CLI starts
        serving traffic.
        """
        # VACUUM cannot be executed inside a transaction.
        with self._lock:
            # Best-effort WAL checkpoint first, then VACUUM.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as exc:
                logger.debug("pre-vacuum WAL checkpoint failed: %s", exc)
            self._conn.execute("VACUUM")

    def maybe_auto_prune_and_vacuum(
        self,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Idempotent auto-maintenance: prune old sessions + optional VACUUM.

        Records the last run timestamp in state_meta so subsequent calls
        within ``min_interval_hours`` no-op. Designed to be called once at
        startup from long-lived entrypoints (CLI, gateway, cron scheduler).

        When *sessions_dir* is provided, on-disk transcript files
        (``.json`` / ``.jsonl`` / ``request_dump_*``) for pruned sessions
        are removed as part of the same sweep (issue #3015).

        Never raises. On any failure, logs a warning and returns a dict
        with ``"error"`` set.

        Returns a dict with keys:
          - ``"skipped"`` (bool) — true if within min_interval_hours of last run
          - ``"pruned"`` (int)   — number of sessions deleted
          - ``"vacuumed"`` (bool) — true if VACUUM ran
          - ``"error"`` (str, optional) — present only on failure
        """
        result: Dict[str, Any] = {"skipped": False, "pruned": 0, "vacuumed": False}
        try:
            # Skip if another process/call did maintenance recently.
            last_raw = self.get_meta("last_auto_prune")
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError) as exc:
                    logger.debug("invalid last_auto_prune value %r: %s", last_raw, exc)

            pruned = self.prune_sessions(
                older_than_days=retention_days,
                sessions_dir=sessions_dir,
            )
            result["pruned"] = pruned

            # Only VACUUM if we actually freed rows — VACUUM on a tight DB
            # is wasted I/O. Threshold keeps small DBs from paying the cost.
            if vacuum and pruned > 0:
                try:
                    self.vacuum()
                    result["vacuumed"] = True
                except Exception as exc:
                    logger.warning("state.db VACUUM failed: %s", exc)

            # Record the attempt even if pruned == 0, so we don't retry
            # every startup within the min_interval_hours window.
            self.set_meta("last_auto_prune", str(now))

            if pruned > 0:
                logger.info(
                    "state.db auto-maintenance: pruned %d session(s) older than %d days%s",
                    pruned,
                    retention_days,
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            # Maintenance must never block startup. Log and return error marker.
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)

        return result

    def maybe_auto_compact_run_events(
        self,
        min_interval_hours: int = 24,
        vacuum: bool = True,
    ) -> Dict[str, Any]:
        """Idempotent run-event maintenance for token-stream storage.

        Live streaming emits token-sized events, but the durable run history
        should keep terminal and structural events, not token replay rows.
        This maintenance is separate from session pruning so desktop/profile
        runtimes can reclaim old chunk rows even when session retention pruning
        is disabled.
        """
        result: Dict[str, Any] = {
            "skipped": False,
            "deleted_events": 0,
            "compacted_segments": 0,
            "pruned_terminal_stream_events": 0,
            "deduplicated_terminal_groups": 0,
            "updated_events": 0,
            "vacuumed": False,
        }
        try:
            last_raw = self.get_meta("last_auto_run_event_compaction_v1")
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError) as exc:
                    logger.debug("invalid last_compact value %r: %s", last_raw, exc)

            compacted = self.compact_run_events()
            result.update({
                "deleted_events": int(compacted.get("deleted_events") or 0),
                "compacted_segments": int(compacted.get("compacted_segments") or 0),
                "pruned_terminal_stream_events": int(compacted.get("pruned_terminal_stream_events") or 0),
                "deduplicated_terminal_groups": int(compacted.get("deduplicated_terminal_groups") or 0),
                "updated_events": int(compacted.get("updated_events") or 0),
            })
            if vacuum and result["deleted_events"] > 0:
                try:
                    self.vacuum()
                    result["vacuumed"] = True
                except Exception as exc:
                    logger.warning("state.db run-event VACUUM failed: %s", exc)
            self.set_meta("last_auto_run_event_compaction_v1", str(now))
            if result["deleted_events"] > 0:
                logger.info(
                    "state.db run-event maintenance: compacted %d segment(s), deleted %d event row(s)%s",
                    result["compacted_segments"],
                    result["deleted_events"],
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            logger.warning("state.db run-event maintenance failed: %s", exc)
            result["error"] = str(exc)
        return result
