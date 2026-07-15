"""Cross-platform session handoff state facade."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from hermes_agent.repositories.session_repo import SessionRepoImpl


class SessionHandoffStateMixin:
    # ── Handoff (cross-platform session transfer) ──────────────────────────
    #
    # State machine:
    #   None       — no handoff in flight
    #   "pending"  — CLI requested handoff, gateway hasn't picked it up yet
    #   "running"  — gateway is processing (session switch + synthetic turn)
    #   "completed"— gateway successfully delivered the synthetic turn
    #   "failed"   — gateway hit an error; reason in handoff_error
    #
    # The CLI writes "pending" then poll-waits for terminal state. The gateway
    # watcher transitions pending→running→{completed,failed}.

    def request_handoff(self, session_id: str, platform: str) -> bool:
        """Mark a session as pending handoff to the given platform.

        Returns True if the row was found and not already in flight; False if
        the session is already in a non-terminal handoff state.
        """
        def _do(conn):
            return SessionRepoImpl(conn).request_handoff(session_id, platform)
        return self._execute_write(_do)

    def get_handoff_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Read the current handoff state for a session.

        Returns ``{"state", "platform", "error"}`` or None if the session has
        no handoff record.
        """
        try:
            cur = self._conn.execute(
                "SELECT handoff_state, handoff_platform, handoff_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "state": row["handoff_state"],
                "platform": row["handoff_platform"],
                "error": row["handoff_error"],
            }
        except sqlite3.OperationalError:
            return None

    def list_pending_handoffs(self) -> List[Dict[str, Any]]:
        """Return all sessions in handoff_state='pending', oldest first.

        Used by the gateway's handoff watcher.
        """
        try:
            cur = self._conn.execute(
                "SELECT * FROM sessions "
                "WHERE handoff_state = 'pending' "
                "ORDER BY started_at ASC"
            )
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    def claim_handoff(self, session_id: str) -> bool:
        """Atomically transition pending → running. Returns True if claimed."""
        def _do(conn):
            return SessionRepoImpl(conn).claim_handoff(session_id)
        return self._execute_write(_do)

    def complete_handoff(self, session_id: str) -> None:
        """Mark a handoff as completed."""
        def _do(conn):
            SessionRepoImpl(conn).complete_handoff(session_id)
        self._execute_write(_do)

    def fail_handoff(self, session_id: str, error: str) -> None:
        """Mark a handoff as failed and record the reason."""
        def _do(conn):
            SessionRepoImpl(conn).fail_handoff(session_id, error[:500])
        self._execute_write(_do)
