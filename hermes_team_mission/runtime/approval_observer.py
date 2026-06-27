"""Bridges in-process approval/clarify state-change events from
``tools.approval`` and ``tools.clarify_gateway`` to a team mission conversation
status projection refresh.

Without this bridge, the sidebar only learned about *plan-level* approval gates
(approval_gate nodes in the mission graph). Member-node tool approvals and
clarify requests live entirely in process memory and never reached the
``team_mission.conversation.status`` event stream, so the sidebar showed
"running" the whole time the composer was actually blocking on the user.

The state-change observers fire on (session_key, present) transitions. We map
the session_key back to a team mission conversation by inspecting
``team_mission_run_bindings`` and ``team_mission_conversations.stable_session_id``.
If we find a hit, we ask the DB to append a fresh conversation.status event so
the supervisor relays it to the FE. Best-effort: any error during the bridge
must not break the approval/clarify flow.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

_log = logging.getLogger(__name__)
_install_lock = threading.Lock()
_installed = False


def install() -> None:
    """Register state-change observers with approval + clarify gateways.

    Idempotent — safe to call multiple times (e.g. on sidecar restart). Both
    observers funnel through the same projection refresh path.
    """
    global _installed
    # Belt + suspenders: write to stderr too. setup_logging() (which attaches
    # the agent.log file handler) is only called when the first AIAgent
    # initializes — install() runs much earlier from main_async, so any
    # logging.warning here is silently dropped on the floor. Stderr always
    # reaches the sidecar's stdout/err console, so we see the install result
    # regardless of when logging gets set up.
    import sys as _sys
    def _say(msg: str) -> None:
        try:
            print(msg, file=_sys.stderr, flush=True)
        except Exception:
            pass
        try:
            _log.warning(msg)
        except Exception:
            pass

    with _install_lock:
        if _installed:
            _say("[doxie-approval-observer] install skipped (already installed)")
            return
        approval_ok = False
        clarify_ok = False
        try:
            from tools import approval as _approval_module
        except Exception as exc:
            _say(f"[doxie-approval-observer] tools.approval import FAILED: {exc}")
            _approval_module = None
        try:
            from tools import clarify_gateway as _clarify_module
        except Exception as exc:
            _say(f"[doxie-approval-observer] tools.clarify_gateway import FAILED: {exc}")
            _clarify_module = None
        if _approval_module is not None and hasattr(_approval_module, "register_state_change_observer"):
            try:
                _approval_module.register_state_change_observer(_on_approval_state_change)
                approval_ok = True
            except Exception as exc:
                _say(f"[doxie-approval-observer] approval register FAILED: {exc}")
        else:
            _say(
                f"[doxie-approval-observer] approval module missing register_state_change_observer (module={_approval_module})"
            )
        if _clarify_module is not None and hasattr(_clarify_module, "register_state_change_observer"):
            try:
                _clarify_module.register_state_change_observer(_on_clarify_state_change)
                clarify_ok = True
            except Exception as exc:
                _say(f"[doxie-approval-observer] clarify register FAILED: {exc}")
        else:
            _say(
                f"[doxie-approval-observer] clarify module missing register_state_change_observer (module={_clarify_module})"
            )
        _installed = True
        _say(
            f"[doxie-approval-observer] install done approval_ok={approval_ok} clarify_ok={clarify_ok}"
        )


def _stderr_log(msg: str) -> None:
    """Belt + suspenders write that survives early-startup logging-not-yet-configured."""
    import sys as _sys
    try:
        print(msg, file=_sys.stderr, flush=True)
    except Exception:
        pass
    try:
        _log.warning(msg)
    except Exception:
        pass


def _on_approval_state_change(session_key: str, present: bool) -> None:
    _stderr_log(f"[doxie-approval-observer] approval state-change session_key={session_key} present={present}")
    _project_state(session_key, present=present, source_event_type="approval.request" if present else "approval.resolved")


def _on_clarify_state_change(session_key: str, present: bool) -> None:
    _stderr_log(f"[doxie-approval-observer] clarify state-change session_key={session_key} present={present}")
    _project_state(session_key, present=present, source_event_type="clarify.request" if present else "clarify.resolved")


def _project_state(session_key: str, *, present: bool, source_event_type: str) -> None:
    """Project an in-process tool-approval / clarify state-change onto the
    canonical sidebar state.

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
        _stderr_log(f"[doxie-approval-observer] server import FAILED: {exc}")
        return
    try:
        db = _server._get_db()
    except Exception as exc:
        _stderr_log(f"[doxie-approval-observer] _get_db FAILED: {exc}")
        db = None
    if db is None:
        _stderr_log(f"[doxie-approval-observer] db is None for session_key={session_key}")
        return

    pending_updater = getattr(db, "update_session_index_pending_state_for_session_key", None)
    if callable(pending_updater):
        try:
            rows = pending_updater(session_key, waiting_approval=present)
            _stderr_log(
                f"[doxie-approval-observer] index_update session_key={session_key} waiting={present} rows={rows}"
            )
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
        _stderr_log(
            "[doxie-approval-observer] db.update_session_index_pending_state_for_session_key missing (older backend?)"
        )

    mission_ids = _missions_for_session_key(db, session_key)
    _stderr_log(
        f"[doxie-approval-observer] session_key={session_key} resolved missions={mission_ids} event={source_event_type}"
    )
    if not mission_ids:
        return
    appender = getattr(db, "append_team_mission_conversation_status_event", None)
    if not callable(appender):
        _stderr_log("[doxie-approval-observer] db.append_team_mission_conversation_status_event missing")
        return
    source_event = {
        "type": source_event_type,
        "payload": {"session_key": session_key},
    }
    for mission_id in mission_ids:
        try:
            result = appender(mission_id=mission_id, source_event=source_event, source_mission_seq=0)
            _stderr_log(
                f"[doxie-approval-observer] appended status event mission={mission_id} result_type={type(result).__name__}"
            )
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
    1. session_key == team_mission_conversations.stable_session_id (leader session).
    2. session_key == team_mission_run_bindings.session_id (member node session).
    3. session_key == team_mission_run_bindings.runtime_session_id (worker runtime).
    """
    found: list[str] = []
    seen: set[str] = set()
    try:
        conn = getattr(db, "_conn", None)
        if conn is None:
            return found
        try:
            lock = getattr(db, "_lock", threading.Lock())
            lock_acquired = lock.acquire(timeout=2.0) if hasattr(lock, "acquire") else False
        except Exception:
            lock_acquired = False
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT tm.mission_id
                  FROM team_missions tm
                  JOIN team_mission_conversations tmc
                    ON tmc.conversation_id = tm.conversation_id
                 WHERE tmc.stable_session_id = ?
                 UNION
                SELECT DISTINCT mission_id
                  FROM team_mission_run_bindings
                 WHERE session_id = ?
                    OR runtime_session_id = ?
                    OR runtime_scope_key = ?
                """,
                (session_key, session_key, session_key, session_key),
            ).fetchall()
        finally:
            if lock_acquired:
                try:
                    lock.release()
                except Exception:
                    pass
        for row in rows or []:
            mid = row[0] if not isinstance(row, dict) else row.get("mission_id")
            mid = str(mid or "").strip()
            if mid and mid not in seen:
                seen.add(mid)
                found.append(mid)
    except Exception as exc:
        _log.debug("mission lookup failed for session_key=%s: %s", session_key, exc)
    return found
