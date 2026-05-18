from __future__ import annotations

from collections.abc import Callable, Mapping


def notify_session_boundary(event_type: str, session_id: str | None) -> None:
    try:
        from hermes_cli.plugins import invoke_hook as invoke_hook

        invoke_hook(event_type, session_id=session_id, platform="tui")
    except Exception:
        pass


def finalize_session(
    session: dict | None,
    *,
    end_reason: str,
    get_db: Callable[[], object | None],
    notify: Callable[[str, str | None], None],
) -> None:
    if not session or session.get("_finalized"):
        return
    session["_finalized"] = True

    agent = session.get("agent")
    lock = session.get("history_lock")
    if lock is not None:
        with lock:
            history = list(session.get("history", []))
    else:
        history = list(session.get("history", []))
    if agent is not None and history and hasattr(agent, "commit_memory_session"):
        try:
            agent.commit_memory_session(history)
        except Exception:
            pass

    session_key = session.get("session_key")
    session_id = getattr(agent, "session_id", None) or session_key
    notify("on_session_finalize", session_id)

    if session_id:
        try:
            db = get_db()
            if db is not None:
                db.end_session(session_id, end_reason)
        except Exception:
            pass


def shutdown_sessions(
    sessions: Mapping[str, dict],
    finalize: Callable[[dict | None], None],
) -> None:
    for session in list(sessions.values()):
        finalize(session)
        try:
            worker = session.get("slash_worker")
            if worker:
                worker.close()
        except Exception:
            pass
