"""ADR-0001 Phase 0 backfill — populate ``run_events.activity_id`` on legacy rows.

The schema migration that introduces ``run_events.activity_id`` leaves all
pre-existing rows NULL. The runtime cannot rely on the column for queries
until those rows have a stable value, but we also cannot block the gateway
on a multi-second SQL pass at every startup. This module runs ONCE per
state.db, in the background, with a state_meta marker so subsequent
startups skip it cheaply.

The inference order intentionally trusts cheap signals before reaching for
the expensive ``runs`` join:

1. If the frame stored in ``event_json`` already carries an
   ``activity_id`` (frames written after the Phase 0 codec landed but
   before the column was populated retroactively), use it.
2. If the row's ``session_id`` matches the team-mission node pattern
   ``team:mission-<X>:node:<Y>``, derive ``activity_id = "mission:<X>"``.
3. If the row's ``session_id`` matches the leader conversation pattern
   ``team-session-team-conversation-<X>``, derive
   ``activity_id = "team-conversation:<X>"``.
4. Otherwise, treat the row as single-agent chat and use
   ``activity_id = f"chat:{session_id}"``.

This produces a stable, query-able activity_id for every legacy row
without depending on the ``runs`` table being fully consistent. Phase 1
will start writing canonical activity_id values via the Activity Command
bus; the backfill values remain valid for the historical replay path.

The pass is bounded to ``MAX_BATCH_ROWS`` per cycle so a very large
database does not block the maintenance loop. The marker stores the
highest ``id`` already inspected so the next cycle can resume cheaply.
Once ``id >= COALESCE(MAX(id), 0)`` no more work remains and the
``done`` marker is written.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

from hermes_agent.domain.event_ledger import EventLedger

logger = logging.getLogger(__name__)

# Marker keys in ``state_meta`` (Hermes' generic kv table).
_PROGRESS_KEY = "activity_id_backfill_v1_max_id"
_DONE_KEY = "activity_id_backfill_v1_done"

# Inferred id prefixes — keep in sync with ADR-0001 § Phase 0 inference.
_PREFIX_MISSION = "mission:"
_PREFIX_TEAM_CONV = "team-conversation:"
_PREFIX_CHAT = "chat:"

# Bound a single backfill pass so it does not stall the maintenance loop.
MAX_BATCH_ROWS = 5000

_NODE_SESSION_PATTERN = re.compile(r"^team:(mission-[A-Za-z0-9]+):node:")
_LEADER_SESSION_PATTERN = re.compile(r"^team-session-team-conversation-([A-Za-z0-9-]+)$")


def _activity_id_from_session_id(session_id: str) -> str:
    """Cheap structural inference from the session_id string."""
    sid = (session_id or "").strip()
    if not sid:
        return ""
    match = _NODE_SESSION_PATTERN.match(sid)
    if match:
        return f"{_PREFIX_MISSION}{match.group(1)}"
    match = _LEADER_SESSION_PATTERN.match(sid)
    if match:
        return f"{_PREFIX_TEAM_CONV}{match.group(1)}"
    return f"{_PREFIX_CHAT}{sid}"


def _activity_id_from_event_json(event_json: Any) -> str:
    """Extract activity_id from the persisted frame, if present.

    Frames stamped after ADR-0001 _apply_run_context_to_frame landed
    carry activity_id at the top level (or inside payload/metadata),
    even if the dedicated column was not populated retroactively.
    """
    if not event_json:
        return ""
    try:
        frame = json.loads(event_json) if isinstance(event_json, (str, bytes)) else event_json
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(frame, dict):
        return ""
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    metadata = frame.get("metadata") if isinstance(frame.get("metadata"), dict) else {}
    return str(
        frame.get("activity_id")
        or frame.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
        or metadata.get("activity_id")
        or metadata.get("activityId")
        or ""
    ).strip()


def _activity_id_for_row(row: Any) -> str:
    """Return the best-effort activity_id for a legacy run_events row."""
    event_json = row["event_json"] if "event_json" in row.keys() else None
    inferred = _activity_id_from_event_json(event_json)
    if inferred:
        return inferred
    session_id = row["session_id"] if "session_id" in row.keys() else ""
    return _activity_id_from_session_id(session_id)


def _read_meta(db: Any, key: str) -> Optional[str]:
    getter = getattr(db, "get_meta", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception:
        return None


def _write_meta(db: Any, key: str, value: str) -> None:
    setter = getattr(db, "set_meta", None)
    if not callable(setter):
        return
    try:
        setter(key, value)
    except Exception as exc:
        logger.debug("activity_id backfill set_meta(%s) failed: %s", key, exc)


def backfill_run_events_activity_id(
    db: Any,
    *,
    dry_run: bool = False,
    batch_size: int = MAX_BATCH_ROWS,
) -> Dict[str, Any]:
    """Populate ``run_events.activity_id`` for legacy rows.

    Returns a dict with:
      - ``skipped``  — True if backfill is marked done in state_meta
      - ``scanned``  — number of NULL rows inspected this cycle
      - ``updated``  — number of rows UPDATEd
      - ``done``     — True if no NULL rows remain after this cycle
      - ``next_max_id`` — the highest ``id`` seen this cycle (resume hint)

    The function never raises on inference failure; a row that produces an
    empty inferred id is left NULL so a subsequent pass can retry with
    richer signals.
    """
    started = time.monotonic()
    if _read_meta(db, _DONE_KEY):
        return {"skipped": True, "reason": "done", "scanned": 0, "updated": 0}

    conn = getattr(db, "_conn", None)
    lock = getattr(db, "_lock", None)
    if conn is None or lock is None:
        return {"skipped": True, "reason": "db_handle_missing"}

    try:
        last_max_id_raw = _read_meta(db, _PROGRESS_KEY) or "0"
        last_max_id = int(last_max_id_raw)
    except (TypeError, ValueError):
        last_max_id = 0

    with lock:
        rows = conn.execute(
            """
            SELECT id, session_id, event_json
            FROM run_events
            WHERE activity_id IS NULL
              AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_max_id, max(1, int(batch_size))),
        ).fetchall()

    scanned = len(rows)
    updates: list[Tuple[str, int]] = []
    next_max_id = last_max_id
    for row in rows:
        row_id = int(row["id"]) if "id" in row.keys() else 0
        next_max_id = max(next_max_id, row_id)
        activity_id = _activity_id_for_row(row)
        if activity_id:
            updates.append((activity_id, row_id))

    updated = 0
    if updates and not dry_run:
        with lock:
            try:
                ledger = EventLedger(conn)
                for activity_id, row_id in updates:
                    ledger.mark_activity_id(row_id=row_id, activity_id=activity_id)
                conn.commit()
                updated = len(updates)
            except Exception as exc:
                logger.warning(
                    "activity_id backfill batch UPDATE failed (rows=%d): %s",
                    len(updates),
                    exc,
                )
                # Do not advance the progress marker on failure — next cycle
                # will retry the same id range.
                return {
                    "skipped": False,
                    "scanned": scanned,
                    "updated": 0,
                    "done": False,
                    "error": str(exc),
                    "duration_s": time.monotonic() - started,
                }

    if not dry_run and next_max_id > last_max_id:
        _write_meta(db, _PROGRESS_KEY, str(next_max_id))

    # done = no rows remained to scan this cycle (we hit the end of the table)
    done = scanned == 0 or scanned < batch_size and not _has_more_nulls(db, next_max_id, lock=lock, conn=conn)
    if done and not dry_run:
        _write_meta(db, _DONE_KEY, str(int(time.time())))
        logger.info(
            "activity_id backfill complete (final_max_id=%d, total_updated_this_cycle=%d)",
            next_max_id,
            updated,
        )

    return {
        "skipped": False,
        "scanned": scanned,
        "updated": updated,
        "done": done,
        "next_max_id": next_max_id,
        "duration_s": time.monotonic() - started,
    }


def _has_more_nulls(db: Any, threshold_id: int, *, lock: Any, conn: Any) -> bool:
    """Check whether any NULL activity_id rows remain past ``threshold_id``."""
    with lock:
        row = conn.execute(
            "SELECT 1 FROM run_events WHERE activity_id IS NULL AND id > ? LIMIT 1",
            (int(threshold_id),),
        ).fetchone()
    return row is not None
