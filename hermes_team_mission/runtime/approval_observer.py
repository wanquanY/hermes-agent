"""Project clarify/approval events onto sidebar pending state.

Worker subprocesses report clarify and approval transitions through the
runtime event stream. The main-process worker frame router calls the public
projection helper here after publishing those events, so sidebar state is
derived from the same cross-process channel that renders the request cards.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)


def _observer_log(msg: str, *, level: int = logging.DEBUG) -> None:
    """Route observer diagnostics through Hermes logging, not stderr."""
    try:
        _log.log(level, msg)
    except Exception:
        pass


def project_clarify_or_approval_state(session_key: str, *, present: bool, source_event_type: str) -> None:
    """Public: called by worker_frame_router on clarify.* / approval.* events."""
    _project_state(session_key, present=present, source_event_type=source_event_type)


def _project_state(session_key: str, *, present: bool, source_event_type: str) -> None:
    """Project a tool-approval / clarify event onto the canonical sidebar state.

    Two writes (both best-effort, neither blocks/breaks the source flow):

    1. ``update_session_index_pending_state_for_session_key`` — UPDATEs the
       ``waiting_approval`` / ``running`` columns on every session_index
       row reachable from ``session_key`` (normal conv, team conv via
       stable id, team conv via leader scope, team member node). This is
       what the sidebar reads.

    2. ``append_team_mission_conversation_status_event`` — for the team
       mission case ONLY (resolved via mission lookup), append a fresh
       ``team_mission.conversation.status`` event so the supervisor pushes
       the new conversation state to the renderer immediately instead of
       waiting for the next list-poll. Normal conversations rely on the
       regular session-index push channel.
    """
    if not session_key:
        return
    try:
        from tui_gateway import server as _server
    except Exception as exc:
        _observer_log(f"[doxie-approval-observer] server import FAILED: {exc}", level=logging.WARNING)
        return
    try:
        db = _server._get_db()
    except Exception as exc:
        _observer_log(f"[doxie-approval-observer] _get_db FAILED: {exc}", level=logging.WARNING)
        db = None
    if db is None:
        _observer_log(
            f"[doxie-approval-observer] db is None for session_key={session_key}",
            level=logging.WARNING,
        )
        return

    pending_updater = getattr(db, "update_session_index_pending_state_for_session_key", None)
    if callable(pending_updater):
        try:
            pending_updater(session_key, waiting_approval=present)
        except Exception as exc:
            _log.warning(
                "[doxie-approval-observer] db.%s FAILED session_key=%s waiting=%s error_type=%s error=%s",
                "update_session_index_pending_state_for_session_key",
                session_key,
                present,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
    else:
        _observer_log(
            "[doxie-approval-observer] db.update_session_index_pending_state_for_session_key missing (older backend?)",
            level=logging.WARNING,
        )

    mission_ids = _missions_for_session_key(db, session_key)
    if not mission_ids:
        return
    appender = getattr(db, "append_team_mission_conversation_status_event", None)
    if not callable(appender):
        _observer_log(
            "[doxie-approval-observer] db.append_team_mission_conversation_status_event missing",
            level=logging.WARNING,
        )
        return
    source_event = {
        "type": source_event_type,
        "payload": {"session_key": session_key},
    }
    for mission_id in mission_ids:
        try:
            appender(mission_id=mission_id, source_event=source_event, source_mission_seq=0)
        except Exception as exc:
            _log.warning(
                "[doxie-approval-observer] db.%s FAILED session_key=%s mission=%s error_type=%s error=%s",
                "append_team_mission_conversation_status_event",
                session_key,
                mission_id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )


def _missions_for_session_key(db: Any, session_key: str) -> list[str]:
    """Resolve a session_key to mission_ids whose conversation includes it.

    Three matches considered:
    1. session_key == team_mission_conversations.conversation_session_id (leader session).
    2. session_key == team_mission_run_bindings.session_id (member node session).
    3. session_key == team_mission_run_bindings.execution_session_id (worker runtime).
    """
    try:
        return db.team_missions.mission_ids_for_session_key(session_key)
    except Exception as exc:
        _log.debug("mission lookup failed for session_key=%s: %s", session_key, exc)
        return []
