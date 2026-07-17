from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any


def _dispatch_notification(
    *,
    sid: str,
    session: dict,
    text: str,
    emit: Callable[[str, str, dict | None], Any],
    run_prompt_submit: Callable[[str, str, dict, str], Any],
) -> None:
    with session["history_lock"]:
        if session.get("running"):
            raise RuntimeError("session is running")
        session["running"] = True

    rid = f"__notif__{int(time.time() * 1000)}"
    emit("message.start", sid, None)
    run_prompt_submit(rid, sid, session, text)


def _event_session_key(evt: dict) -> str:
    return str(evt.get("session_key") or "").strip()


def _session_notification_keys(session: dict) -> set[str]:
    keys = {str(session.get("session_key") or "").strip()}
    agent = session.get("agent")
    keys.add(str(getattr(agent, "session_id", "") or "").strip())
    keys.discard("")
    return keys


def session_owns_notification_event(session: dict, evt: dict) -> bool:
    event_key = _event_session_key(evt)
    return bool(event_key and event_key in _session_notification_keys(session))


def notification_poller_loop(
    stop_event: threading.Event,
    sid: str,
    session: dict,
    *,
    emit: Callable[[str, str, dict | None], Any],
    run_prompt_submit: Callable[[str, str, dict, str], Any],
    resolve_event_session: Callable[[dict], tuple[str, dict] | None] | None = None,
) -> None:
    from tools.process_registry import process_registry, format_process_notification

    def handle_event(evt: dict, *, requeue_when_busy: bool) -> bool:
        # Activity/Run events are rendered from durable lifecycle state. They
        # must never be converted into a synthetic prompt turn even if a
        # future producer accidentally places one on the legacy process queue.
        if str(evt.get("type") or "").startswith("activity."):
            return True
        event_sid = evt.get("session_id", "")
        if evt.get("type") == "completion" and process_registry.is_completion_consumed(event_sid):
            return True
        if not _event_session_key(evt):
            return True

        target_sid = sid
        target_session = session
        if not session_owns_notification_event(session, evt):
            resolved = resolve_event_session(evt) if resolve_event_session else None
            if resolved is None:
                return True
            target_sid, target_session = resolved

        text = format_process_notification(evt)
        if not text:
            return True

        emit("status.update", target_sid, {"kind": "process", "text": text})
        if evt.get("type") in {"watch_match", "watch_disabled"}:
            return True

        try:
            _dispatch_notification(
                sid=target_sid,
                session=target_session,
                text=text,
                emit=emit,
                run_prompt_submit=run_prompt_submit,
            )
            return True
        except RuntimeError:
            if requeue_when_busy:
                process_registry.completion_queue.put(evt)
            return False
        except Exception as exc:
            print(
                f"[tui_gateway] notification poller dispatch failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            with session["history_lock"]:
                session["running"] = False
            return True

    while not stop_event.is_set() and not session.get("_finalized"):
        try:
            evt = process_registry.completion_queue.get(timeout=0.5)
        except Exception:
            continue
        handle_event(evt, requeue_when_busy=True)

    while not process_registry.completion_queue.empty():
        try:
            evt = process_registry.completion_queue.get_nowait()
        except Exception:
            break
        if not handle_event(evt, requeue_when_busy=True):
            break


def start_notification_poller(
    sid: str,
    session: dict,
    *,
    emit: Callable[[str, str, dict | None], Any],
    run_prompt_submit: Callable[[str, str, dict, str], Any],
    resolve_event_session: Callable[[dict], tuple[str, dict] | None] | None = None,
) -> threading.Event:
    stop = threading.Event()
    thread = threading.Thread(
        target=notification_poller_loop,
        args=(stop, sid, session),
        kwargs={
            "emit": emit,
            "run_prompt_submit": run_prompt_submit,
            "resolve_event_session": resolve_event_session,
        },
        daemon=True,
    )
    thread.start()
    return stop
