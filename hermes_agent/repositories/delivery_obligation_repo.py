"""Repository for the delivery_obligations aggregate."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Iterable, Optional

from hermes_agent.domain.delivery_obligation import (
    DeliveryObligation,
    DeliveryObligationState,
    RecoverableDelivery,
)


class DeliveryObligationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, obligation: DeliveryObligation) -> bool:
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO delivery_obligations (
                   obligation_id, session_key, platform, chat_id, thread_id,
                   reply_to, metadata_json, content, state, attempts,
                   created_at, updated_at, owner_pid, owner_started_at,
                   last_error
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                obligation.obligation_id,
                obligation.session_key,
                obligation.platform,
                obligation.chat_id,
                obligation.thread_id,
                obligation.reply_to,
                json.dumps(
                    dict(obligation.metadata),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
                obligation.content,
                obligation.state.value,
                obligation.attempts,
                obligation.created_at,
                obligation.updated_at,
                obligation.owner_pid,
                obligation.owner_started_at,
                obligation.last_error,
            ),
        )
        return bool(cursor.rowcount)

    def mark_state(
        self,
        obligation_id: str,
        state: DeliveryObligationState,
        *,
        now: float,
        error: str = "",
    ) -> bool:
        cursor = self._conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?
               WHERE obligation_id=? AND state != 'delivered'""",
            (
                state.value,
                now,
                error[:500] if error else None,
                obligation_id,
            ),
        )
        return bool(cursor.rowcount)

    def claim_recoverable(
        self,
        *,
        now: float,
        owner_pid: int,
        owner_started_at: Optional[int],
        owner_alive: Callable[[Any, Any], bool],
        max_attempts: int,
        stale_after_seconds: float,
        deliverable_platforms: Optional[set[str]] = None,
    ) -> list[RecoverableDelivery]:
        rows = self._conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id,
                      thread_id, reply_to, metadata_json, content, state,
                      attempts, created_at, updated_at, owner_pid,
                      owner_started_at, last_error
               FROM delivery_obligations
               WHERE state IN ('pending', 'attempting', 'failed')"""
        ).fetchall()
        claimed: list[RecoverableDelivery] = []
        for row in rows:
            obligation = _map_row(row)
            if owner_alive(obligation.owner_pid, obligation.owner_started_at):
                continue
            if (
                obligation.attempts >= max_attempts
                or now - obligation.created_at > stale_after_seconds
            ):
                self._conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?
                       WHERE obligation_id=? AND state=?""",
                    (now, obligation.obligation_id, obligation.state.value),
                )
                continue
            if (
                deliverable_platforms is not None
                and obligation.platform not in deliverable_platforms
            ):
                # A claim increments the redelivery budget.  Leave rows for
                # disconnected platforms untouched so every attempt buys an
                # actual platform send; the stale cutoff still bounds them.
                continue
            cursor = self._conn.execute(
                """UPDATE delivery_obligations
                   SET owner_pid=?, owner_started_at=?, attempts=attempts+1,
                       updated_at=?
                   WHERE obligation_id=? AND state=?
                     AND owner_pid IS ? AND owner_started_at IS ?""",
                (
                    owner_pid,
                    owner_started_at,
                    now,
                    obligation.obligation_id,
                    obligation.state.value,
                    obligation.owner_pid,
                    obligation.owner_started_at,
                ),
            )
            if not cursor.rowcount:
                continue
            claimed_obligation = DeliveryObligation(
                **{
                    **_obligation_fields(obligation),
                    "attempts": obligation.attempts + 1,
                    "updated_at": now,
                    "owner_pid": owner_pid,
                    "owner_started_at": owner_started_at,
                }
            )
            claimed.append(
                RecoverableDelivery(
                    obligation=claimed_obligation,
                    needs_duplicate_marker=(
                        obligation.state is not DeliveryObligationState.PENDING
                    ),
                )
            )
        return claimed

    def prune(
        self,
        *,
        now: float,
        retention_seconds: float,
        max_rows: int,
    ) -> None:
        self._conn.execute(
            """DELETE FROM delivery_obligations
               WHERE state IN ('delivered', 'abandoned') AND updated_at < ?""",
            (now - retention_seconds,),
        )
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM delivery_obligations"
        ).fetchone()
        total = int(_row_value(row, "count", 0) or 0)
        excess = max(0, total - max_rows)
        if excess:
            self._conn.execute(
                """DELETE FROM delivery_obligations WHERE obligation_id IN (
                       SELECT obligation_id FROM delivery_obligations
                       ORDER BY CASE state
                                  WHEN 'delivered' THEN 0
                                  WHEN 'abandoned' THEN 1
                                  ELSE 2
                                END,
                                updated_at ASC
                       LIMIT ?
                   )""",
                (excess,),
            )

    def list_recent(self, limit: int = 20) -> list[DeliveryObligation]:
        rows = self._conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id,
                      thread_id, reply_to, metadata_json, content, state,
                      attempts, created_at, updated_at, owner_pid,
                      owner_started_at, last_error
               FROM delivery_obligations
               ORDER BY updated_at DESC LIMIT ?""",
            (max(0, int(limit)),),
        ).fetchall()
        return [_map_row(row) for row in rows]


def _obligation_fields(obligation: DeliveryObligation) -> dict[str, Any]:
    return {
        name: getattr(obligation, name)
        for name in DeliveryObligation.__dataclass_fields__
    }


def _row_value(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, sqlite3.Row) else row[index]


def _map_row(row: Any) -> DeliveryObligation:
    raw_metadata = _row_value(row, "metadata_json", 6)
    try:
        metadata = json.loads(raw_metadata or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return DeliveryObligation(
        obligation_id=str(_row_value(row, "obligation_id", 0)),
        session_key=str(_row_value(row, "session_key", 1)),
        platform=str(_row_value(row, "platform", 2)),
        chat_id=str(_row_value(row, "chat_id", 3)),
        thread_id=_optional_text(_row_value(row, "thread_id", 4)),
        reply_to=_optional_text(_row_value(row, "reply_to", 5)),
        metadata=metadata,
        content=str(_row_value(row, "content", 7)),
        state=DeliveryObligationState(str(_row_value(row, "state", 8))),
        attempts=int(_row_value(row, "attempts", 9) or 0),
        created_at=float(_row_value(row, "created_at", 10)),
        updated_at=float(_row_value(row, "updated_at", 11)),
        owner_pid=_optional_int(_row_value(row, "owner_pid", 12)),
        owner_started_at=_optional_int(
            _row_value(row, "owner_started_at", 13)
        ),
        last_error=_optional_text(_row_value(row, "last_error", 14)),
    )


def _optional_text(value: Any) -> Optional[str]:
    return str(value) if value not in {None, ""} else None


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["DeliveryObligationRepository"]
