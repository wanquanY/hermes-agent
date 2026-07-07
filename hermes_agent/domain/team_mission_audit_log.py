"""Team Mission audit projection access.

``team_mission_events`` is not a timeline source of truth. It is a legacy
audit/read-model projection kept for team-mission diagnostics and historical
inspection while Phase E migrates canonical replay to ``run_events``.

All physical access to the projection is centralized here so callers cannot
accidentally treat the table as a second event ledger.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


@dataclass(frozen=True)
class AuditAppendResult:
    event: dict[str, Any]
    inserted: bool


class TeamMissionAuditLog:
    """Central access point for the legacy ``team_mission_events`` projection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(
        self,
        *,
        mission_id: str,
        dedupe_key: str,
        event: dict[str, Any],
        event_type: str = "",
        source_event_type: str = "",
        source_run_id: str = "",
        source_session_id: str = "",
        source_seq: int = 0,
        timestamp: float | None = None,
        now: float | None = None,
    ) -> AuditAppendResult:
        stable_mission = _text(mission_id)
        stable_dedupe = _text(dedupe_key)
        if not stable_mission or not stable_dedupe or not isinstance(event, dict):
            return AuditAppendResult(event={}, inserted=False)
        existing = self._conn.execute(
            "SELECT event_json FROM team_mission_events WHERE mission_id = ? AND dedupe_key = ?",
            (stable_mission, stable_dedupe),
        ).fetchone()
        if existing is not None:
            duplicate = _row_to_event(existing)
            duplicate["_persistence_disposition"] = "duplicate_mission_event"
            return AuditAppendResult(event=duplicate, inserted=False)
        insert_now = float(now if now is not None else time.time())
        seq = self._allocate_seq(stable_mission, updated_at=insert_now)
        stored = dict(event)
        payload = stored.get("payload") if isinstance(stored.get("payload"), dict) else {}
        payload = dict(payload)
        stored["seq"] = seq
        stored["team_mission_event_seq"] = seq
        stored["teamMissionEventSeq"] = seq
        payload["seq"] = seq
        payload["team_mission_event_seq"] = seq
        payload["teamMissionEventSeq"] = seq
        stored["payload"] = payload
        try:
            self._conn.execute(
                """
                INSERT INTO team_mission_events (
                    mission_id, seq, event_type, source_event_type,
                    source_run_id, source_session_id, source_seq, dedupe_key,
                    timestamp, payload_json, source_event_json, event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_mission,
                    seq,
                    _text(event_type),
                    _text(source_event_type),
                    _text(source_run_id),
                    _text(source_session_id),
                    int(source_seq or 0),
                    stable_dedupe,
                    float(timestamp if timestamp is not None else stored.get("timestamp") or insert_now),
                    "",
                    "",
                    _json_dumps(stored),
                    insert_now,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self._conn.execute(
                "SELECT event_json FROM team_mission_events WHERE mission_id = ? AND dedupe_key = ?",
                (stable_mission, stable_dedupe),
            ).fetchone()
            duplicate = _row_to_event(existing)
            duplicate["_persistence_disposition"] = "duplicate_mission_event"
            return AuditAppendResult(event=duplicate, inserted=False)
        return AuditAppendResult(event=stored, inserted=True)

    def _allocate_seq(self, mission_id: str, *, updated_at: float) -> int:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO team_mission_event_seq_counter (mission_id, next_seq, updated_at)
            VALUES (?, 1, ?)
            """,
            (mission_id, float(updated_at or 0)),
        )
        row = self._conn.execute(
            """
            UPDATE team_mission_event_seq_counter
               SET next_seq = next_seq + 1,
                   updated_at = ?
             WHERE mission_id = ?
            RETURNING next_seq - 1
            """,
            (float(updated_at or 0), mission_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"team_mission_event_seq_counter allocation failed for {mission_id}")
        return int(row[0])

    def list(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        stable_mission = _text(mission_id)
        if not stable_mission:
            return []
        bounded_limit = max(1, min(int(limit or 2000), 10000))
        rows = self._conn.execute(
            """
            SELECT event_json
            FROM team_mission_events
            WHERE mission_id = ?
              AND seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (stable_mission, int(after_seq or 0), bounded_limit),
        ).fetchall()
        return [event for row in rows if (event := _row_to_event(row))]

    def latest_seq(self, mission_id: str) -> int:
        stable_mission = _text(mission_id)
        if not stable_mission:
            return 0
        row = self._conn.execute(
            "SELECT next_seq - 1 AS latest_seq FROM team_mission_event_seq_counter WHERE mission_id = ?",
            (stable_mission,),
        ).fetchone()
        return int((row["latest_seq"] if row is not None else 0) or 0)

    def prune_source_event_types(self, *, mission_id: str, source_event_types: Iterable[str]) -> int:
        stable_mission = _text(mission_id)
        types = [_text(item) for item in source_event_types if _text(item)]
        if not stable_mission or not types:
            return 0
        placeholders = ",".join("?" for _ in types)
        cursor = self._conn.execute(
            f"""
            DELETE FROM team_mission_events
            WHERE mission_id = ?
              AND source_event_type IN ({placeholders})
            """,
            (stable_mission, *types),
        )
        return int(cursor.rowcount or 0)


def _row_to_event(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        raw = row["event_json"]
    except Exception:
        raw = None
    event = _json_loads(raw, {})
    return event if isinstance(event, dict) else {}
