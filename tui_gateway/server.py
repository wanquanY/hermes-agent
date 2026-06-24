import atexit
import concurrent.futures
import contextvars
import copy
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent.dovie_diagnostics import emit_dovie_diagnostic
from hermes_constants import get_hermes_home
from hermes_cli.env_loader import load_hermes_dotenv
from utils import is_truthy_value
from tui_gateway.transport import (
    StdioTransport,
    Transport,
    bind_transport,
    current_transport,
    reset_transport,
)
from tui_gateway.services.crash_logging import install_panic_logger
from tui_gateway.services.media import (
    enrich_with_attached_images as _enrich_with_attached_images,
    estimate_image_tokens as _estimate_image_tokens,
    image_meta as _image_meta,
)
from tui_gateway.services.model_descriptor import (
    normalize_model_descriptor as _normalize_model_descriptor,
    set_session_model_descriptor as _set_session_model_descriptor,
)
from tui_gateway.services.notification_poller import (
    notification_poller_loop as _notification_poller_loop_service,
    session_owns_notification_event as _session_owns_notification_event,
    start_notification_poller as _start_notification_poller_service,
)
from tui_gateway.services.profile_context import (
    active_hermes_home as _active_hermes_home_for_profile,
    enter_profile_context as _enter_profile_context,
    leave_profile_context as _leave_profile_context,
    profile_context_for_params as _profile_context_for_params,
)
from tui_gateway.services import run_control
from tui_gateway.services.session_store import (
    db_unavailable_detail as _db_unavailable_detail,
    get_session_db_for_home as _get_session_db_for_home,
    resolve_home_path as _resolve_home_path,
)
from tui_gateway.services.session_info import (
    get_usage as _get_usage,
    probe_config_health as _probe_config_health,
    probe_credentials as _probe_credentials,
    session_info as _session_info,
)
from tui_gateway.services.slash_worker_client import SlashWorker as _SlashWorker
from tui_gateway.services.tool_events import (
    GatewayToolEventBridge,
    wire_secret_callbacks,
)
from tui_gateway.services.transcript_messages import (
    history_to_messages as _history_to_messages,
    tool_context as _tool_ctx,
)
from dovie_extension import load_extension

logger = logging.getLogger(__name__)


_DOVIE_STREAM_TRACE_EVENTS = {
    "message.start",
    "message.delta",
    "message.complete",
    "reasoning.delta",
    "thinking.delta",
}


def _stream_trace_text(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("delta", "text", "snapshot", "output", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _stream_trace_payload_summary(payload: dict | None) -> dict[str, Any]:
    text = _stream_trace_text(payload)
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return {
        "payload_mode": str((payload or {}).get("mode") or ""),
        "payload_len": len(text),
        "payload_sha1": digest,
        "payload_preview": text[:80].replace("\n", "\\n"),
    }


def _transport_debug_id(transport: Any) -> str:
    if transport is None:
        return ""
    return f"{transport.__class__.__name__}:{id(transport):x}"


def _trace_stream_route(stage: str, **fields: Any) -> None:
    emit_dovie_diagnostic("[dovie-stream-route]", {"stage": stage, **fields})


def _diagnostic_param_summary(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {"param_type": type(params).__name__}
    keys = sorted(str(key) for key in params.keys())
    summary: dict[str, Any] = {"param_keys": keys[:40], "param_key_count": len(keys)}
    for key in (
        "identifier",
        "mission_id",
        "missionId",
        "conversation_id",
        "conversationId",
        "conversation_session_id",
        "conversationSessionId",
        "stable_team_session_id",
        "stableTeamSessionId",
        "node_id",
        "nodeId",
        "session_id",
        "sessionId",
        "stored_session_id",
        "storedSessionId",
        "runtime_scope_key",
        "runtimeScopeKey",
        "profile_runtime_scope_key",
        "profileRuntimeScopeKey",
        "agent_profile_id",
        "agentProfileId",
        "agent_profile_version_id",
        "agentProfileVersionId",
        "include_run_events",
        "includeRunEvents",
        "limit",
        "run_events_limit",
        "runEventsLimit",
    ):
        value = params.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        summary[key] = str(value)[:240]
    return summary

_hermes_home = get_hermes_home()
load_hermes_dotenv(
    hermes_home=_hermes_home, project_env=Path(__file__).parent.parent / ".env"
)


_CRASH_LOG = install_panic_logger(_hermes_home)

try:
    from hermes_cli.banner import prefetch_update_check

    prefetch_update_check()
except Exception:
    pass

from tui_gateway.render import make_stream_renderer, render_diff, render_message

_sessions: dict[str, dict] = {}
_methods: dict[str, callable] = {}
_pending: dict[str, tuple[str, threading.Event]] = {}
_answers: dict[str, str] = {}
_db = None
_db_error: str | None = None
_db_by_home: dict[str, object] = {}
_db_error_by_home: dict[str, str] = {}
_GATEWAY_INSTANCE_ID = uuid.uuid4().hex
_stdout_lock = threading.Lock()
_cfg_lock = threading.Lock()
# RLock so the helpers below (_attach_worker, _close_session_by_id) can be
# called from inside an outer `with _sessions_lock` block without
# self-deadlocking. Ported from upstream ae94ed172 (fix(tui-gateway): reap
# leaked slash_worker sessions on disconnect).
_sessions_lock = threading.RLock()
_prompt_lock = threading.Lock()
_session_resume_lock = threading.Lock()
_profile_env_lock = threading.RLock()
_cfg_cache: dict | None = None
_cfg_mtime: float | None = None
_cfg_path = None
_DETAIL_SECTION_NAMES = ("thinking", "tools", "subagents", "activity")
_DETAIL_MODES = frozenset({"hidden", "collapsed", "expanded"})
_READ_ONLY_DB_METHODS = frozenset(
    {
        "artifacts.list",
        "conversation.activity.list",
        "delegation.status",
        "events.subscribe",
        "insights.get",
        "profile.draft.get",
        "profile.draft.list",
        "profile.get",
        "profile.growth.summary",
        "profile.list",
        "rollback.diff",
        "rollback.list",
        "run.events",
        "run.list",
        "run.status",
        "subagent.events.list",
        "subagent.runs.list",
        "session.history",
        "session.list",
        "session.messages",
        "session.most_recent",
        "session.status",
        "session.usage",
        "team_mission.conversation.list",
        "team_mission.conversation.render",
        "team_mission.conversation.resolve",
        "spawn_tree.list",
        "workspace.current",
        "workspace.session.current",
        "workspace.session.list",
        "workspace.list",
    }
)


def _agent_context_mode_from_params(params: dict | None = None) -> str:
    raw = (
        (params or {}).get("agent_context_mode")
        or (params or {}).get("agentContextMode")
        or (params or {}).get("runtime_context_mode")
        or (params or {}).get("runtimeContextMode")
        or ""
    )
    mode = str(raw or "").strip().lower().replace("-", "_")
    if mode in {"team_leader", "leader_conversation"}:
        return "team_leader"
    if mode in {"profile", "profile_conversation", "default"}:
        return "profile"
    return ""


def _agent_context_options_for_session(session: dict | None) -> dict:
    """Resolve semantic context loading policy for a gateway runtime session."""

    mode = _agent_context_mode_from_params(session)
    ignore_rules = is_truthy_value(os.environ.get("HERMES_IGNORE_RULES"))
    options = {
        "skip_context_files": ignore_rules,
        "skip_memory": ignore_rules,
    }
    if mode == "team_leader":
        options["load_soul_identity"] = True
    return options


def _log_agent_build_stage(sid: str, session: dict | None, stage: str, **fields: Any) -> None:
    session = session or {}
    runtime_scope_key = str(
        session.get("active_runtime_scope_key")
        or session.get("runtime_scope_key")
        or session.get("session_key")
        or sid
    )
    run_id = str(session.get("active_run_id") or "")
    if not run_id and not runtime_scope_key.startswith("team:"):
        return
    pairs = {
        "stage": stage,
        "sid": sid,
        "stored_session_id": str(session.get("session_key") or sid),
        "run_id": run_id,
        "turn_id": str(session.get("active_turn_id") or ""),
        "runtime_scope_key": runtime_scope_key,
        **fields,
    }
    emit_dovie_diagnostic("[dovie-agent-build-stage]", pairs)
_current_method: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tui_gateway_current_method",
    default="",
)
# ── Async RPC dispatch (#12546) ──────────────────────────────────────
# A handful of handlers block the dispatcher loop in entry.py for seconds
# to minutes (slash.exec, cli.exec, shell.exec, session.resume,
# session.branch, session.compress, skills.manage).  While they're running, inbound RPCs —
# notably approval.respond and session.interrupt — sit unread in the
# stdin pipe.  We route only those slow handlers onto a small thread pool;
# everything else stays on the main thread so ordering stays sane for the
# fast path.  write_json is already _stdout_lock-guarded, so concurrent
# response writes are safe.
_LONG_HANDLERS = frozenset(
    {
        "browser.manage",
        "cli.exec",
        "session.branch",
        "session.compress",
        "session.resume",
        "shell.exec",
        "skills.manage",
        "slash.exec",
    }
)

try:
    _rpc_pool_workers = max(
        2, int(os.environ.get("HERMES_TUI_RPC_POOL_WORKERS") or "4")
    )
except (ValueError, TypeError):
    _rpc_pool_workers = 4
_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=_rpc_pool_workers,
    thread_name_prefix="tui-rpc",
)
atexit.register(lambda: _pool.shutdown(wait=False, cancel_futures=True))

# Reserve real stdout for JSON-RPC only; redirect Python's stdout to stderr
# so stray print() from libraries/tools becomes harmless gateway.stderr instead
# of corrupting the JSON protocol.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

# Module-level stdio transport — fallback sink when no transport is bound via
# contextvar or session. Stream resolved through a lambda so runtime monkey-
# patches of `_real_stdout` (used extensively in tests) still land correctly.
_stdio_transport = StdioTransport(lambda: _real_stdout, _stdout_lock)

_DOVIE_EXTENSION = load_extension()
_EXTRACTED_METHOD_OVERRIDES = _DOVIE_EXTENSION.gateway_method_overrides()


def _load_busy_input_mode() -> str:
    display = _load_cfg().get("display")
    if not isinstance(display, dict):
        display = {}
    raw = str(display.get("busy_input_mode", "") or "").strip().lower()
    return raw if raw in {"queue", "steer", "interrupt"} else "interrupt"


def _notify_session_boundary(event_type: str, session_id: str | None) -> None:
    """Fire session lifecycle hooks with CLI parity."""
    try:
        from hermes_cli.plugins import invoke_hook as _invoke_hook

        _invoke_hook(event_type, session_id=session_id, platform="tui")
    except Exception:
        pass


def _finalize_session(
    session: dict | None,
    end_reason: str = "tui_close",
    *,
    runtime_sid: str = "",
) -> None:
    """Best-effort finalize hook + memory commit for a session."""
    if not session or session.get("_finalized"):
        return
    session["_finalized"] = True
    profile_tokens = _enter_profile_context(session.get("profile_context"), apply_env=False)
    stop_event = session.get("_notif_stop")
    try:
        if stop_event is not None:
            stop_event.set()

        _terminalize_active_run_for_shutdown(
            session,
            end_reason=end_reason,
            runtime_sid=runtime_sid,
        )

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
        _notify_session_boundary("on_session_finalize", session_id)

        # Mark session ended in DB so it doesn't linger as a ghost row in /resume.
        # Use session_id (from agent.session_id) not session_key — after compression,
        # session_key may be stale (the ended parent) while session_id is the live
        # continuation. Fix for #20001.
        if session_id:
            try:
                db = _db_for_stable_session(str(session_id or session_key or ""))
                if db is not None:
                    db.end_session(session_id, end_reason)
            except Exception:
                pass
    finally:
        _leave_profile_context(profile_tokens)


def _terminalize_active_run_for_shutdown(
    session: dict,
    *,
    end_reason: str,
    runtime_sid: str = "",
) -> None:
    stable_session_id = str(session.get("session_key") or runtime_sid or "").strip()
    if not stable_session_id:
        return
    db = _db_for_stable_session(stable_session_id)
    if db is None:
        return
    run_id = str(session.get("active_run_id") or "").strip()
    turn_id = str(session.get("active_turn_id") or "").strip()
    runtime_scope_key = str(
        session.get("active_runtime_scope_key")
        or session.get("runtime_scope_key")
        or stable_session_id
    ).strip()
    if not run_id:
        try:
            status = run_control.session_status(
                stable_session_id,
                db=db,
                current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
            )
            run_id = str(status.get("active_run_id") or "").strip()
            turn_id = str(status.get("active_turn_id") or turn_id or "").strip()
            runtime_scope_key = str(
                status.get("runtime_scope_key")
                or runtime_scope_key
                or stable_session_id
            ).strip()
        except Exception:
            logger.warning(
                "[dovie-gateway] shutdown active-run lookup failed session_id=%s",
                stable_session_id,
                exc_info=True,
            )
            return
    if not run_id:
        return
    message = f"gateway {end_reason} before run reached terminal state"
    logger.warning(
        "[dovie-gateway] terminalizing active run during shutdown session_id=%s run_id=%s turn_id=%s reason=%s",
        stable_session_id,
        run_id,
        turn_id,
        end_reason,
    )
    run_control.publish_run_terminal_event(
        stored_session_id=stable_session_id,
        run_id=run_id,
        turn_id=turn_id,
        runtime_scope_key=runtime_scope_key or stable_session_id,
        runtime_session_id=str(runtime_sid or "").strip(),
        status="interrupted",
        message=message,
        db=db,
        owner_transport=current_transport(),
    )


def _shutdown_sessions() -> None:
    with _sessions_lock:
        sessions = list(_sessions.items())
    for runtime_sid, session in sessions:
        _finalize_session(session, end_reason="tui_shutdown", runtime_sid=runtime_sid)
        try:
            worker = session.get("slash_worker")
            if worker:
                worker.close()
        except Exception:
            pass


atexit.register(_shutdown_sessions)


# ── Plumbing ──────────────────────────────────────────────────────────


def _get_db():
    global _db, _db_error
    active_home = _resolve_home_path(_active_hermes_home, fallback=_hermes_home)
    default_home = _resolve_home_path(_hermes_home, fallback=_hermes_home)
    create_if_missing = _current_method.get("") not in _READ_ONLY_DB_METHODS
    result = _get_session_db_for_home(
        active_home=active_home,
        default_home=default_home,
        default_db=_db,
        default_error=_db_error,
        db_by_home=_db_by_home,
        db_error_by_home=_db_error_by_home,
        logger=logger,
        create_if_missing=create_if_missing,
    )
    _db = result.default_db
    _db_error = result.default_error
    return result.db


def _is_control_plane_stable_session_id(stable_session_id: str) -> bool:
    stable = str(stable_session_id or "").strip()
    return stable.startswith(
        (
            "team:mission-",
            "team-session-team-conversation-",
            "team-conversation-",
        )
    )


def _get_control_plane_db():
    global _db, _db_error
    default_home = _resolve_home_path(_hermes_home, fallback=_hermes_home)
    create_if_missing = _current_method.get("") not in _READ_ONLY_DB_METHODS
    result = _get_session_db_for_home(
        active_home=default_home,
        default_home=default_home,
        default_db=_db,
        default_error=_db_error,
        db_by_home=_db_by_home,
        db_error_by_home=_db_error_by_home,
        logger=logger,
        create_if_missing=create_if_missing,
    )
    _db = result.default_db
    _db_error = result.default_error
    return result.db


def _db_for_stable_session(stable_session_id: str):
    if _is_control_plane_stable_session_id(stable_session_id):
        return _get_control_plane_db()
    return _get_db()


def _db_unavailable_error(rid, *, code: int):
    try:
        active_home = _resolve_home_path(_active_hermes_home, fallback=_hermes_home)
    except Exception:
        active_home = None
    detail = _db_unavailable_detail(
        active_home=active_home,
        default_error=_db_error,
        db_error_by_home=_db_error_by_home,
    )
    return _err(rid, code, f"state.db unavailable: {detail}")


# --- session lifecycle helpers (ported from upstream ae94ed172) ---
#
# These close two long-standing leak / race classes in the tui-gateway:
#
# C1 (disconnect reap): when a websocket transport dies (uvicorn drops the
#     socket on a half-open peer, reverse-proxy returns 524, the renderer
#     hard-quits), every session attached to that transport must reach a
#     single, idempotent teardown chokepoint — otherwise the
#     `session.active_list` count grows monotonically until the gateway is
#     restarted, slash_worker subprocesses leak, and tools/approval keeps
#     dispatching to a dead WS notify hook. See _close_sessions_for_transport
#     and _close_session_by_id below.
#
# C2 (create/close race): the slash_worker spawn path is *not* under the
#     sessions lock (subprocess start is slow; we don't want to block
#     anything else taking the lock). Between "spawn worker" and "attach
#     worker to session dict", a concurrent teardown can pop the session,
#     in which case the freshly-spawned worker would be orphaned forever
#     (no one holds a reference, but the subprocess is still running and
#     holding its PTY). _attach_worker re-checks under the lock and closes
#     the worker if the session is gone.
#
# Idempotency: every teardown path (session.close, ws disconnect, idle
# reaper, shutdown, ws-orphan-reap) reaches _close_session_by_id ->
# _finalize_session, and _finalize_session is guarded by the `_finalized`
# flag (already present), so concurrent / repeat calls are no-ops.


def _attach_worker(sid: str, session: dict, worker) -> None:
    """Store ``worker`` on ``session`` iff ``sid`` still maps to it.

    Closes the create/close race (C2): between spawning the slash_worker
    subprocess and adding it to ``session["slash_worker"]``, a concurrent
    teardown can pop ``_sessions[sid]``. Without this re-check we'd leak
    the worker subprocess (and its PTY). Caller must already have spawned
    the worker; this function decides whether to keep or close it.

    Call sites: every place that calls ``_SlashWorker(...)`` then assigns
    it to a session dict — line ~945, ~1661 (_restart_slash_worker),
    ~2376 (session.create dovie path).
    """
    with _sessions_lock:
        if _sessions.get(sid) is session:
            session["slash_worker"] = worker
            return
    # Session was popped concurrently — worker is now an orphan. Close it
    # outside the lock since worker.close() can block on subprocess wait.
    try:
        worker.close()
    except Exception:
        pass


def _close_session_by_id(sid: str, *, end_reason: str = "tui_close") -> bool:
    """Single idempotent teardown for one session.

    Pops the session under ``_sessions_lock`` (RLock — safe to re-enter via
    _finalize_session, which itself doesn't take this lock but is sometimes
    called from inside `with _sessions_lock` blocks elsewhere). The
    ``_finalized`` guard inside _finalize_session makes concurrent or
    repeat calls (session.close racing the WS-orphan reaper) harmless.

    Returns True iff this call popped a live session — useful when callers
    want to log "closed by reaper" vs "already gone".
    """
    with _sessions_lock:
        session = _sessions.pop(sid, None)
    if session is None:
        return False
    runtime_sid = session.get("runtime_session_id") or ""
    _finalize_session(session, end_reason=end_reason, runtime_sid=runtime_sid)
    # tools.approval can hold a notify callback bound to this session_key
    try:
        from tools.approval import unregister_gateway_notify

        key = session.get("session_key")
        if key:
            unregister_gateway_notify(key)
    except Exception:
        pass
    # Close agent + slash worker. Best effort: failures are logged, not
    # raised, so a partial teardown still progresses through the rest.
    try:
        agent = session.get("agent")
        if agent is not None and hasattr(agent, "close"):
            agent.close()
    except Exception:
        pass
    # Slash worker close is *also* done inside _finalize_session via the
    # _finalized chokepoint; this is a belt-and-suspenders for the rare
    # path where finalize raised before reaching that step.
    try:
        worker = session.get("slash_worker")
        if worker is not None and hasattr(worker, "close"):
            worker.close()
    except Exception:
        pass
    return True


def _close_sessions_for_transport(
    transport, *, end_reason: str = "ws_disconnect"
) -> tuple[int, int]:
    """C1 disconnect reap: when a transport dies, reap sessions attached to it.

    Sessions that opted in (via ``close_on_disconnect=True`` — set by the
    dovie sidecar and the dashboard embed) are torn down immediately. The
    rest are merely *detached* from the dead transport (their next emit
    would otherwise hit a closed socket); a higher-level orphan-reaper
    sweeps them on a grace window.

    Returns (reaped, detached) for caller logging.

    The actual teardown is offloaded to ``asyncio.to_thread`` by the caller
    in ws.py because worker.close() + DB write inside _finalize_session can
    take 50-200ms; running it inline would stall the uvicorn event loop.
    """
    reaped: list[str] = []
    detached: list[str] = []
    with _sessions_lock:
        # snapshot under lock, mutate after
        for sid, session in list(_sessions.items()):
            if session.get("transport") is not transport:
                continue
            if session.get("close_on_disconnect"):
                reaped.append(sid)
            else:
                session["transport"] = None
                detached.append(sid)
    for sid in reaped:
        _close_session_by_id(sid, end_reason=end_reason)
    return len(reaped), len(detached)


def write_json(obj: dict) -> bool:
    """Emit one JSON frame. Routes via the most-specific transport available.

    Precedence:

    1. Event frames with a session id → the transport stored on that session,
       so async events land with the client that owns the session even if
       the emitting thread has no contextvar binding.
    2. Otherwise the transport bound on the current context (set by
       :func:`dispatch` for the lifetime of a request).
    3. Otherwise the module-level stdio transport, matching the historical
       behaviour and keeping tests that monkey-patch ``_real_stdout`` green.
    """
    if obj.get("method") == "event":
        sid = ((obj.get("params") or {}).get("session_id")) or ""
        with _sessions_lock:
            transport = (_sessions.get(sid) or {}).get("transport") if sid else None
        if transport is not None:
            return transport.write(obj)

    return (current_transport() or _stdio_transport).write(obj)


def _emit(event: str, sid: str, payload: dict | None = None):
    params = {"type": event, "session_id": sid}
    event_payload = payload or {}
    stable_session_id = ""
    run_id = ""
    turn_id = ""
    runtime_scope_key = ""
    direct_transport = None
    try:
        from tui_gateway.services import run_control

        with _sessions_lock:
            session = _sessions.get(sid) or {}
        stable_session_id = str(session.get("session_key") or sid or "")
        run_id = str(event_payload.get("run_id") or session.get("active_run_id") or "")
        turn_id = str(event_payload.get("turn_id") or session.get("active_turn_id") or "")
        runtime_scope_key = str(
            event_payload.get("runtime_scope_key")
            or session.get("active_runtime_scope_key")
            or session.get("runtime_scope_key")
            or stable_session_id
        )
        if stable_session_id:
            params["stored_session_id"] = stable_session_id
        if run_id:
            params["run_id"] = run_id
        if turn_id:
            params["turn_id"] = turn_id
        if runtime_scope_key:
            params["runtime_scope_key"] = runtime_scope_key
        if sid:
            params["runtime_session_id"] = sid
        session_transport = session.get("transport")
        context_transport = current_transport()
        direct_transport = session_transport or context_transport or _stdio_transport
        if stable_session_id and run_id:
            event_db = _db_for_stable_session(stable_session_id)
            frame = {
                "type": event,
                "session_id": sid,
                "stored_session_id": stable_session_id,
                "run_id": run_id,
                "turn_id": turn_id,
                "runtime_session_id": sid,
                "runtime_scope_key": runtime_scope_key,
                "owner_metadata": {
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                },
                "payload": event_payload,
            }
            frame["seq"] = run_control.next_event_seq(stable_session_id, db=event_db)
            params["seq"] = frame["seq"]
            terminal_event = _is_terminal_run_event(event)
            recorded_deliveries = run_control.publish_recorded_event(
                frame,
                db=event_db,
                owner_transport=direct_transport,
                skip_owner_transport=True,
                before_deliver=(lambda: _release_terminal_session_run(sid, run_id))
                if terminal_event
                else None,
            )
            if event in _DOVIE_STREAM_TRACE_EVENTS:
                _trace_stream_route(
                    "record-publish",
                    event_type=event,
                    session_id=sid,
                    stored_session_id=stable_session_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    runtime_scope_key=runtime_scope_key,
                    seq=frame["seq"],
                    session_transport=_transport_debug_id(session_transport),
                    context_transport=_transport_debug_id(context_transport),
                    owner_transport=_transport_debug_id(direct_transport),
                    subscriber_delivery_count=len(recorded_deliveries or []),
                    **_stream_trace_payload_summary(event_payload),
                )
    except Exception:
        logger.warning(
            "[dovie-gateway] emit record failed event=%s session_id=%s stored_session_id=%s run_id=%s turn_id=%s runtime_scope_key=%s",
            event,
            sid,
            stable_session_id,
            run_id,
            turn_id,
            runtime_scope_key,
            exc_info=True,
        )
    if payload is not None:
        params["payload"] = payload
    direct_delivered = write_json({"jsonrpc": "2.0", "method": "event", "params": params})
    if event in _DOVIE_STREAM_TRACE_EVENTS:
        _trace_stream_route(
            "direct-write",
            event_type=event,
            session_id=sid,
            stored_session_id=stable_session_id,
            run_id=run_id,
            turn_id=turn_id,
            runtime_scope_key=runtime_scope_key,
            seq=params.get("seq") or 0,
            direct_transport=_transport_debug_id(direct_transport),
            delivered=bool(direct_delivered),
            **_stream_trace_payload_summary(event_payload),
        )
    if direct_delivered:
        try:
            from tui_gateway.services import run_control

            run_control.remember_transport_delivery(direct_transport, params)
        except Exception:
            logger.debug("failed to remember direct event delivery", exc_info=True)


def _is_terminal_run_event(event: str) -> bool:
    return str(event or "").strip() in {
        "message.complete",
        "error",
        "session.interrupted",
    }


def _release_terminal_session_run(sid: str, run_id: str) -> None:
    runtime_sid = str(sid or "").strip()
    completed_run_id = str(run_id or "").strip()
    if not runtime_sid or not completed_run_id:
        return
    with _sessions_lock:
        session = _sessions.get(runtime_sid)
    if not isinstance(session, dict):
        return
    if str(session.get("active_run_id") or "") != completed_run_id:
        return
    session["running"] = False
    session["active_run_id"] = None
    session["active_turn_id"] = None
    session["pending_turn"] = None
    session["run_updated_at"] = time.time()


def _active_hermes_home():
    try:
        default_home = get_hermes_home()
    except Exception:
        default_home = _hermes_home
    return _active_hermes_home_for_profile(
        fallback=_hermes_home,
        default_home=str(default_home),
    )


def _status_update(sid: str, kind: str, text: str | None = None):
    body = (text if text is not None else kind).strip()
    if not body:
        return
    _emit(
        "status.update",
        sid,
        {"kind": kind if text is not None else "status", "text": body},
    )


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def method(name: str):
    def dec(fn):
        if str(getattr(fn, "__module__", "")).startswith("tui_gateway.methods."):
            _methods[name] = fn
        else:
            _methods.setdefault(name, fn)
        return fn

    return dec


def _normalize_request(req: Any) -> tuple[Any, str, dict] | dict:
    """Validate a JSON-RPC request enough for safe local dispatch."""
    if not isinstance(req, dict):
        return _err(None, -32600, "invalid request: expected an object")

    rid = req.get("id")
    method = req.get("method")
    if not isinstance(method, str) or not method:
        return _err(rid, -32600, "invalid request: method must be a non-empty string")

    params = req.get("params", {})
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return _err(rid, -32602, "invalid params: expected an object")

    return rid, method, params


def handle_request(req: dict) -> dict | None:
    normalized = _normalize_request(req)
    if isinstance(normalized, dict):
        return normalized

    rid, method, params = normalized
    fn = _methods.get(method)
    if (
        fn is None
        or (
            method in _EXTRACTED_METHOD_OVERRIDES
            and str(getattr(fn, "__module__", "")) == __name__
        )
    ):
        _register_extracted_method_modules()
        fn = _methods.get(method)
    if not fn:
        return _err(rid, -32601, f"unknown method: {method}")
    profile_token = _enter_profile_context(_profile_context_for_params(params))
    method_token = _current_method.set(method)
    try:
        return fn(rid, params)
    finally:
        _current_method.reset(method_token)
        _leave_profile_context(profile_token)


def dispatch(req: dict, transport: Optional[Transport] = None) -> dict | None:
    """Route inbound RPCs — long handlers to the pool, everything else inline.

    Returns a response dict when handled inline. Returns None when the
    handler was scheduled on the pool; the worker writes its own response
    via the bound transport when done.

    *transport* (optional): pins every write produced by this request —
    including any events emitted by the handler — to the given transport.
    Omitting it falls back to the module-level stdio transport, preserving
    the original behaviour for ``tui_gateway.entry``.
    """
    t = transport or _stdio_transport
    token = bind_transport(t)
    try:
        normalized = _normalize_request(req)
        if isinstance(normalized, dict):
            return normalized

        rid, method, params = normalized

        def run_handler() -> dict | None:
            try:
                return handle_request(req)
            except Exception as exc:
                diagnostic = {
                    "method": method,
                    "request_id": rid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    **_diagnostic_param_summary(params),
                }
                emit_dovie_diagnostic("[tui-gateway-handler-error]", diagnostic)
                logger.exception("[tui-gateway] handler error method=%s diagnostic=%s", method, diagnostic)
                return _err(rid, -32000, f"handler error: {exc}")

        if method not in _LONG_HANDLERS:
            return run_handler()

        # Snapshot the context so the pool worker sees the bound transport.
        ctx = contextvars.copy_context()

        def run():
            resp = run_handler()
            if resp is not None:
                t.write(resp)

        _pool.submit(lambda: ctx.run(run))

        return None
    finally:
        reset_transport(token)


def _wait_agent(session: dict, rid: str, timeout: float = 30.0) -> dict | None:
    ready = session.get("agent_ready")
    if ready is not None:
        _log_agent_build_stage(str(session.get("runtime_session_id") or ""), session, "wait-start", timeout=timeout)
        if not ready.wait(timeout=timeout):
            _log_agent_build_stage(str(session.get("runtime_session_id") or ""), session, "wait-timeout", timeout=timeout)
            return _err(rid, 5032, "agent initialization timed out")
        _log_agent_build_stage(str(session.get("runtime_session_id") or ""), session, "wait-end")
    err = session.get("agent_error")
    if err:
        _log_agent_build_stage(str(session.get("runtime_session_id") or ""), session, "wait-error", error=err)
    return _err(rid, 5032, err) if err else None


def _start_agent_build(sid: str, session: dict) -> None:
    """Start building the real AIAgent for a TUI session, once.

    Classic `hermes` shows the prompt before constructing AIAgent; the TUI used
    to eagerly build it during session.create, making startup feel blocked on
    tool discovery/model metadata even though the composer was visible.  Keep
    the shell responsive by deferring this work until the first prompt (or any
    command that actually needs the agent), while retaining the same ready/error
    event contract for the frontend.
    """
    ready = session.get("agent_ready")
    if ready is None:
        return
    lock = session.setdefault("agent_build_lock", threading.Lock())
    with lock:
        if ready.is_set() or session.get("agent_build_started"):
            _log_agent_build_stage(sid, session, "start-skip", ready=ready.is_set(), started=bool(session.get("agent_build_started")))
            return
        session["agent_build_started"] = True
    key = session["session_key"]
    session["runtime_session_id"] = sid
    _log_agent_build_stage(sid, session, "start")

    def _build() -> None:
        started_at = time.time()
        with _sessions_lock:
            current = _sessions.get(sid)
        if current is None:
            _log_agent_build_stage(sid, session, "session-missing")
            ready.set()
            return

        worker = None
        notify_registered = False
        try:
            cwd = current.get("cwd")
            _log_agent_build_stage(sid, current, "thread-entry", cwd=cwd)
            _log_agent_build_stage(sid, current, "profile-context-enter-start")
            profile_tokens = _enter_profile_context(current.get("profile_context"))
            _log_agent_build_stage(sid, current, "profile-context-enter-end")
            _log_agent_build_stage(sid, current, "session-context-enter-start")
            tokens = _set_session_context(key, terminal_cwd=cwd)
            try:
                _log_agent_build_stage(sid, current, "make-agent-start")
                agent = _make_agent(sid, key, cwd=cwd)
                _log_agent_build_stage(
                    sid,
                    current,
                    "make-agent-end",
                    model=str(getattr(agent, "model", "") or ""),
                    agent_session_id=str(getattr(agent, "session_id", "") or ""),
                )
            finally:
                _clear_session_context(tokens)
                _leave_profile_context(profile_tokens)
                _log_agent_build_stage(sid, current, "context-exit")

            # Session DB row deferred to first run_conversation() call.
            # pending_title applied post-first-message (see cli.exec handler).
            current["agent"] = agent

            try:
                _log_agent_build_stage(sid, current, "slash-worker-start")
                worker = _SlashWorker(key, getattr(agent, "model", _resolve_model()))
                # C2: re-check sid -> current under the lock before storing
                # the worker; a concurrent teardown could have popped this
                # session between agent build and now.
                _attach_worker(sid, current, worker)
                _log_agent_build_stage(sid, current, "slash-worker-end")
            except Exception:
                _log_agent_build_stage(sid, current, "slash-worker-error")
                pass

            try:
                from tools.approval import (
                    register_gateway_notify,
                    load_permanent_allowlist,
                )

                register_gateway_notify(
                    key, lambda data: _emit("approval.request", sid, data)
                )
                notify_registered = True
                load_permanent_allowlist()
            except Exception:
                pass

            _log_agent_build_stage(sid, current, "wire-callbacks-start")
            _wire_callbacks(sid)
            _log_agent_build_stage(sid, current, "wire-callbacks-end")
            with _sessions_lock:
                if sid in _sessions:
                    _log_agent_build_stage(sid, current, "notification-poller-start")
                    _sessions[sid]["_notif_stop"] = _start_notification_poller(sid, _sessions[sid])
                    _log_agent_build_stage(sid, current, "notification-poller-end")
            _log_agent_build_stage(sid, current, "session-boundary-notify-start")
            _notify_session_boundary("on_session_reset", key)
            _log_agent_build_stage(sid, current, "session-boundary-notify-end")

            _log_agent_build_stage(sid, current, "session-info-start")
            info = _session_info(agent, current)
            _log_agent_build_stage(sid, current, "credential-probe-start")
            warn = _probe_credentials(agent)
            _log_agent_build_stage(sid, current, "credential-probe-end", has_warning=bool(warn))
            if warn:
                info["credential_warning"] = warn
            _log_agent_build_stage(sid, current, "config-health-probe-start")
            cfg_warn = _probe_config_health(_load_cfg())
            _log_agent_build_stage(sid, current, "config-health-probe-end", has_warning=bool(cfg_warn))
            if cfg_warn:
                info["config_warning"] = cfg_warn
                logger.warning(cfg_warn)
            _log_agent_build_stage(sid, current, "session-info-emit-start")
            _emit("session.info", sid, info)
            _log_agent_build_stage(
                sid,
                current,
                "thread-complete",
                elapsed_ms=int((time.time() - started_at) * 1000),
            )
        except Exception as e:
            current["agent_error"] = str(e)
            _log_agent_build_stage(sid, current, "thread-error", error=str(e))
            _emit("error", sid, {"message": f"agent init failed: {e}"})
        finally:
            with _sessions_lock:
                replaced = _sessions.get(sid) is not current
            if replaced:
                if worker is not None:
                    try:
                        worker.close()
                    except Exception:
                        pass
                if notify_registered:
                    try:
                        from tools.approval import unregister_gateway_notify

                        unregister_gateway_notify(key)
                    except Exception:
                        pass
            ready.set()
            _log_agent_build_stage(sid, current, "ready-set")

    threading.Thread(target=_build, daemon=True).start()


def _sess_nowait(params, rid):
    runtime_sid, session = _resolve_runtime_session(params.get("session_id") or "")
    if session is None:
        return (None, _err(rid, 4001, "session not found"))
    params["session_id"] = runtime_sid
    return (session, None)


def _sess(params, rid):
    s, err = _sess_nowait(params, rid)
    if err:
        return (None, err)
    _start_agent_build(params.get("session_id") or "", s)
    return (s, _wait_agent(s, rid))


# ── Config I/O ────────────────────────────────────────────────────────


# Keep aligned with `INDICATOR_STYLES` / `DEFAULT_INDICATOR_STYLE` in
# ``ui-tui/src/app/interfaces.ts`` — both ends validate against the
# same shape so `config.get indicator` and the live TUI render agree.
_INDICATOR_STYLES: tuple[str, ...] = ("ascii", "emoji", "kaomoji", "unicode")
_INDICATOR_DEFAULT = "kaomoji"


def _load_cfg() -> dict:
    global _cfg_cache, _cfg_mtime, _cfg_path
    try:
        import yaml

        p = Path(_hermes_home) / "config.yaml"
        mtime = p.stat().st_mtime if p.exists() else None
        with _cfg_lock:
            if _cfg_cache is not None and _cfg_mtime == mtime and _cfg_path == p:
                return copy.deepcopy(_cfg_cache)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        with _cfg_lock:
            _cfg_cache = copy.deepcopy(data)
            _cfg_mtime = mtime
            _cfg_path = p
        return data
    except Exception:
        pass
    return {}


def _save_cfg(cfg: dict):
    global _cfg_cache, _cfg_mtime, _cfg_path
    import yaml

    path = Path(_hermes_home) / "config.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
    with _cfg_lock:
        _cfg_cache = copy.deepcopy(cfg)
        _cfg_path = path
        try:
            _cfg_mtime = path.stat().st_mtime
        except Exception:
            _cfg_mtime = None


def _set_session_context(
    session_key: str,
    *,
    terminal_cwd: str | None = None,
    dovie_product_context: str | None = None,
) -> list:
    try:
        from gateway.session_context import set_session_vars

        with _sessions_lock:
            session = next(
                (value for value in _sessions.values() if value.get("session_key") == session_key),
                {},
            )
        return set_session_vars(
            session_key=session_key,
            terminal_cwd=str(terminal_cwd if terminal_cwd is not None else session.get("cwd") or ""),
            dovie_product_context=str(
                dovie_product_context
                if dovie_product_context is not None
                else session.get("dovie_product_context") or ""
            ),
            dovie_browser_session_id=_dovie_browser_session_id(session_key),
        )
    except Exception:
        return []


def _dovie_browser_session_id(session_key: str) -> str:
    from dovie_extension.browser_bridge import browser_session_id_for_gateway_session

    return browser_session_id_for_gateway_session(session_key)


def _clear_session_context(tokens: list) -> None:
    if not tokens:
        return
    try:
        from gateway.session_context import clear_session_vars

        clear_session_vars(tokens)
    except Exception:
        pass


def _session_cwd(session: dict | None = None) -> str:
    from tui_gateway.services.workspace import session_cwd

    return session_cwd(session)


def _enable_gateway_prompts() -> None:
    """Route approvals through gateway callbacks instead of CLI input()."""
    os.environ["HERMES_GATEWAY_SESSION"] = "1"
    os.environ["HERMES_EXEC_ASK"] = "1"
    os.environ["HERMES_INTERACTIVE"] = "1"


# ── Blocking prompt factory ──────────────────────────────────────────


def _block(event: str, sid: str, payload: dict, timeout: int = 300) -> str:
    rid = uuid.uuid4().hex[:8]
    ev = threading.Event()
    with _prompt_lock:
        _pending[rid] = (sid, ev)
        payload["request_id"] = rid
    _emit(event, sid, payload)
    # Project pending state AFTER emit so the FE receives the event before
    # the sidebar flips — preserves the "popup shows, then spinner becomes
    # waiting badge" intuition for users watching both views.
    _project_block_state(sid, present=True)
    try:
        ev.wait(timeout=timeout)
    finally:
        _project_block_state(sid, present=False)
    with _prompt_lock:
        _pending.pop(rid, None)
        return _answers.pop(rid, "")
    try:
        # Diagnostic — short-lived. Captures every event type that flows
        # through _block so we can tell at a glance whether a missing
        # popup is a backend (event not emitted) or frontend (event
        # arrived but no handler) issue. We also dump the session keys
        # that _emit will derive runtime_scope_key / stored_session_id
        # from, because subscription filtering downstream rejects events
        # whose runtime_scope_key doesn't match the FE-side scope key,
        # and that mismatch is invisible from the event_type alone.
        import sys as _sys
        _choices_n = len((payload or {}).get("choices") or []) if isinstance(payload, dict) else 0
        try:
            with _sessions_lock:
                _sess = dict(_sessions.get(sid) or {})
        except Exception:
            _sess = {}
        _line = (
            f"[doxie-block-enter] event={event} sid={sid} rid={rid} "
            f"choices={_choices_n} timeout={timeout} "
            f"session_key={_sess.get('session_key') or ''!r} "
            f"active_runtime_scope_key={_sess.get('active_runtime_scope_key') or ''!r} "
            f"runtime_scope_key={_sess.get('runtime_scope_key') or ''!r} "
            f"active_run_id={_sess.get('active_run_id') or ''!r}"
        )
        print(_line, file=_sys.stderr, flush=True)
        logger.warning(_line)
    except Exception:
        pass
    # Project pending-input state to the canonical sidebar truth.
    #
    # This is THE choke point for every blocking user-input prompt in Dovie:
    # clarify.request, sudo.request, secret.request, approval.request etc.
    # Dovie's tool callbacks (see tui_gateway/services/tool_events.py) wire
    # the agent's clarify_callback to ``_block(...)`` instead of the legacy
    # ``tools.clarify_gateway.register`` — so the previous observer that
    # listened on ``clarify_gateway._notify_state_change`` never saw a
    # Dovie clarify and the sidebar stayed on running spinner the whole
    # time the composer was actually blocking.
    #
    # Projecting from here covers every Dovie blocking prompt with one
    # write. Best-effort: any DB issue must NOT alter the block timing.
    _project_block_state(sid, present=True)
    try:
        ev.wait(timeout=timeout)
    finally:
        _project_block_state(sid, present=False)
    with _prompt_lock:
        _pending.pop(rid, None)
        return _answers.pop(rid, "")


def _project_block_state(sid: str, *, present: bool) -> None:
    """Write ``waiting_approval`` to every session_index row the agent's
    blocking-prompt ``sid`` resolves to. Mirrors what
    ``team_mission_approval_observer`` does for the legacy
    ``tools.clarify_gateway`` / ``tools.approval`` paths — but for the
    Dovie-native ``_block`` mechanism.

    The agent's ``sid`` here is the gateway's INTERNAL 8-char hex id
    (e.g. ``1cf7689d``), NOT the conversation's stored_session_id
    (e.g. ``team-session-team-conversation-d254d3d0-…``). The
    session_index table is keyed by stored_session_id, so feeding the
    short sid straight into the resolver matches zero rows. We resolve
    via the gateway's ``_sessions[sid]["session_key"]`` (the stored
    session id) and fall back to the short sid if the lookup fails.

    All resolution paths live in the DB layer
    (``update_session_index_pending_state_for_session_key``) so the same
    five matches (direct session_id, runtime_scope_key, member-node
    bindings, team-conv stable id, leader scope parse) cover every
    conversation type uniformly.
    """
    if not sid:
        return
    try:
        db = _get_db()
    except Exception:
        return
    if db is None:
        return
    updater = getattr(db, "update_session_index_pending_state_for_session_key", None)
    if not callable(updater):
        return

    candidate_keys: list[str] = []
    seen: set[str] = set()

    def _add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            candidate_keys.append(text)

    try:
        session = _sessions.get(sid)
    except Exception:
        session = None
    if isinstance(session, dict):
        _add(session.get("session_key"))
        _add(session.get("stored_session_id"))
        _add(session.get("runtime_scope_key"))
    # Always include the raw sid as the last resort — it might be the
    # stored id itself in non-Dovie code paths, and the resolver is
    # tolerant of unknown keys (returns 0).
    _add(sid)

    for key in candidate_keys:
        try:
            rows = updater(key, waiting_approval=present)
        except Exception:
            # Sidebar projection is best-effort. A schema mismatch or
            # lock contention must never disturb the clarify/approval
            # timing.
            continue
        if rows and rows > 0:
            # First candidate that resolved is the right one; stop so we
            # don't double-write across overlapping rows.
            return


def _clear_pending(sid: str | None = None) -> None:
    """Release pending prompts with an empty answer.

    When *sid* is provided, only prompts owned by that session are
    released — critical for session.interrupt, which must not
    collaterally cancel clarify/sudo/secret prompts on unrelated
    sessions sharing the same tui_gateway process.  When *sid* is
    None, every pending prompt is released (used during shutdown).
    """
    cleared_sids: set[str] = set()
    with _prompt_lock:
        for rid, (owner_sid, ev) in list(_pending.items()):
            if sid is None or owner_sid == sid:
                _answers[rid] = ""
                ev.set()
                if owner_sid:
                    cleared_sids.add(owner_sid)
    # Mirror the unblock into session_index so the sidebar doesn't keep
    # waiting_approval=1 after a session.interrupt cleared every pending
    # prompt under us. The _block(...) finally-clause covers the normal
    # path; this covers external unblock (interrupt, shutdown).
    for owner_sid in cleared_sids:
        _project_block_state(owner_sid, present=False)


# ── Agent factory ────────────────────────────────────────────────────


def resolve_skin() -> dict:
    try:
        from hermes_cli.skin_engine import init_skin_from_config, get_active_skin

        init_skin_from_config(_load_cfg())
        skin = get_active_skin()
        return {
            "name": skin.name,
            "colors": skin.colors,
            "branding": skin.branding,
            "banner_logo": skin.banner_logo,
            "banner_hero": skin.banner_hero,
            "tool_prefix": skin.tool_prefix,
            "help_header": (skin.branding or {}).get("help_header", ""),
        }
    except Exception:
        return {}


def _resolve_model() -> str:
    env = (
        os.environ.get("HERMES_MODEL", "")
        or os.environ.get("HERMES_INFERENCE_MODEL", "")
    ).strip()
    if env:
        return env
    m = _load_cfg().get("model", "")
    if isinstance(m, dict):
        return str(m.get("default", "") or "").strip()
    if isinstance(m, str) and m:
        return m.strip()
    return "anthropic/claude-sonnet-4"


def _resolve_startup_runtime() -> tuple[str, str | None]:
    model = _resolve_model()
    explicit_provider = os.environ.get("HERMES_TUI_PROVIDER", "").strip()
    if explicit_provider:
        return model, explicit_provider

    explicit_model = (
        os.environ.get("HERMES_MODEL", "")
        or os.environ.get("HERMES_INFERENCE_MODEL", "")
    ).strip()
    if not explicit_model:
        return model, None

    try:
        from hermes_cli.models import detect_static_provider_for_model

        cfg = _load_cfg().get("model") or {}
        current_provider = (
            (
                str(cfg.get("provider") or "").strip().lower()
                if isinstance(cfg, dict)
                else ""
            )
            or os.environ.get("HERMES_INFERENCE_PROVIDER", "").strip().lower()
            or "auto"
        )
        detected = detect_static_provider_for_model(explicit_model, current_provider)
        if detected:
            provider, detected_model = detected
            return detected_model, provider
    except Exception:
        pass
    return model, None


# Backfilled from upstream 6de3963e3 (#43702) + 7d938cc5c — referenced by the
# absorbed bc4dbce858 model-switch no-op fix, but the underlying helpers landed
# in those two non-P0 commits. Without them /model raises NameError after a
# successful in-place agent swap.

def _runtime_model_config(agent, existing: dict | None = None) -> dict:
    config = dict(existing or {})
    model = str(getattr(agent, "model", "") or "").strip()
    provider = str(getattr(agent, "provider", "") or "").strip()
    base_url = str(getattr(agent, "base_url", "") or "").strip()
    api_mode = str(getattr(agent, "api_mode", "") or "").strip()
    reasoning_config = getattr(agent, "reasoning_config", None)
    service_tier = getattr(agent, "service_tier", None)

    if model:
        config["model"] = model
    if provider:
        config["provider"] = provider
    if base_url:
        config["base_url"] = base_url
    else:
        config.pop("base_url", None)
    if api_mode:
        config["api_mode"] = api_mode
    else:
        config.pop("api_mode", None)
    if isinstance(reasoning_config, dict):
        config["reasoning_config"] = reasoning_config
    else:
        config.pop("reasoning_config", None)
    if service_tier:
        config["service_tier"] = service_tier
    else:
        config.pop("service_tier", None)

    return config


def _persist_live_session_runtime(session: dict | None) -> None:
    """Persist active session runtime so future resumes restore the same footer."""
    if not session:
        return
    agent = session.get("agent")
    session_key = str(session.get("session_key") or "").strip()
    if agent is None or not session_key:
        return

    db = getattr(agent, "_session_db", None) or _get_db()
    if db is None:
        return

    try:
        row = db.get_session(session_key) or {}
        raw_config = row.get("model_config")
        existing_config = {}
        if isinstance(raw_config, dict):
            existing_config = raw_config
        elif isinstance(raw_config, str) and raw_config.strip():
            parsed = json.loads(raw_config)
            if isinstance(parsed, dict):
                existing_config = parsed
        model_config = _runtime_model_config(agent, existing_config)
        model = str(getattr(agent, "model", "") or "").strip()
        if hasattr(db, "update_session_meta"):
            db.update_session_meta(session_key, json.dumps(model_config), model or None)
        elif model and hasattr(db, "update_session_model"):
            db.update_session_model(session_key, model)
    except Exception:
        logger.debug("failed to persist live session runtime", exc_info=True)


def _persist_live_session_system_prompt(session: dict | None) -> None:
    """Refresh the stored system prompt after a live runtime identity change."""
    if not session:
        return
    agent = session.get("agent")
    session_key = str(session.get("session_key") or "").strip()
    if agent is None or not session_key or not hasattr(agent, "_build_system_prompt"):
        return

    db = getattr(agent, "_session_db", None) or _get_db()
    if db is None or not hasattr(db, "update_system_prompt"):
        return

    try:
        prompt = agent._build_system_prompt(None)
        agent._cached_system_prompt = prompt
        db.update_system_prompt(getattr(agent, "session_id", None) or session_key, prompt)
    except Exception:
        logger.debug("failed to persist live session system prompt", exc_info=True)


def _append_model_switch_marker(session: dict | None, *, model: str, provider: str) -> None:
    """Record a real system-history pivot after a live model switch."""
    if not session:
        return
    session_key = str(session.get("session_key") or "").strip()
    if not session_key:
        return

    provider_part = f" via provider {provider}" if provider else ""
    marker = (
        "[System: The active model for this chat has changed to "
        f"{model}{provider_part}. From this point forward, use this runtime "
        "metadata when answering questions about what model/provider is active.]"
    )
    entry = {"role": "system", "content": marker}

    lock = session.get("history_lock")
    if lock is not None:
        with lock:
            session.setdefault("history", []).append(entry)
            session["history_version"] = int(session.get("history_version", 0)) + 1
    else:
        session.setdefault("history", []).append(entry)
        session["history_version"] = int(session.get("history_version", 0)) + 1

    try:
        agent = session.get("agent")
        db = getattr(agent, "_session_db", None) if agent is not None else None
        if db is not None:
            db.append_message(session_id=session_key, role="system", content=marker)
            return

        if "_ensure_session_db_row" in globals():
            _ensure_session_db_row(session)
        if "_session_db" in globals():
            with _session_db(session) as scoped_db:
                if scoped_db is not None:
                    scoped_db.append_message(
                        session_id=session_key, role="system", content=marker
                    )
    except Exception:
        logger.debug("failed to persist model switch marker", exc_info=True)


def _write_config_key(key_path: str, value):
    cfg = _load_cfg()
    current = cfg
    keys = key_path.split(".")
    for key in keys[:-1]:
        if key not in current or not isinstance(current.get(key), dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
    _save_cfg(cfg)


_STATUSBAR_MODES = frozenset({"off", "top", "bottom"})


def _coerce_statusbar(raw) -> str:
    if raw is False:
        return "off"
    if isinstance(raw, str) and (s := raw.strip().lower()) in _STATUSBAR_MODES:
        return s
    return "top"


_MOUSE_TRACKING_ALIASES = {
    "0": "off",
    "1": "all",
    "all": "all",
    "any": "all",
    "button": "buttons",
    "buttons": "buttons",
    "click": "buttons",
    "false": "off",
    "full": "all",
    "no": "off",
    "off": "off",
    "on": "all",
    "scroll": "wheel",
    "true": "all",
    "wheel": "wheel",
    "yes": "all",
}


def _display_mouse_tracking(display: dict) -> str:
    """Resolve display.mouse_tracking to one of ``off|wheel|buttons|all``.

    Boolean values keep their legacy meaning (``True`` → ``all``, ``False`` →
    ``off``). The ``wheel`` preset (DEC 1000+1006) is the tmux-friendly
    subset — wheel + click only, no hover events to trigger prompt-row
    clipboard probes. Legacy ``tui_mouse`` is honored only when
    ``mouse_tracking`` is absent.
    """
    if not isinstance(display, dict):
        return "all"
    if "mouse_tracking" in display:
        raw = display.get("mouse_tracking")
    else:
        raw = display.get("tui_mouse", True)
    if raw is False or raw == 0:
        return "off"
    if raw is True or raw is None:
        return "all"
    if isinstance(raw, (int, float)):
        return "all"
    if isinstance(raw, str):
        return _MOUSE_TRACKING_ALIASES.get(raw.strip().lower(), "all")
    return "all"


def _load_reasoning_config() -> dict | None:
    from hermes_constants import parse_reasoning_effort

    effort = str(
        (_load_cfg().get("agent") or {}).get("reasoning_effort", "") or ""
    ).strip()
    return parse_reasoning_effort(effort)


def _load_service_tier() -> str | None:
    raw = (
        str((_load_cfg().get("agent") or {}).get("service_tier", "") or "")
        .strip()
        .lower()
    )
    if not raw or raw in {"normal", "default", "standard", "off", "none"}:
        return None
    if raw in {"fast", "priority", "on"}:
        return "priority"
    return None


def _load_show_reasoning() -> bool:
    return bool((_load_cfg().get("display") or {}).get("show_reasoning", False))


def _load_tool_progress_mode() -> str:
    env = os.environ.get("HERMES_TUI_TOOL_PROGRESS", "").strip().lower()
    if env in {"off", "new", "all", "verbose"}:
        return env
    raw = (_load_cfg().get("display") or {}).get("tool_progress", "all")
    if raw is False:
        return "off"
    if raw is True:
        return "all"
    mode = str(raw or "all").strip().lower()
    return mode if mode in {"off", "new", "all", "verbose"} else "all"


def _load_enabled_toolsets() -> list[str] | None:
    explicit = [
        item.strip()
        for item in os.environ.get("HERMES_TUI_TOOLSETS", "").split(",")
        if item.strip()
    ]
    cfg = None
    fallback_notice = None

    # Coding posture (base Hermes): with no explicit pin, collapse to the
    # coding toolset (+ enabled MCP servers) when sitting in a code workspace.
    # The desktop app and `hermes --tui` both land here. See
    # agent/coding_context.py. No config is loaded yet at this point, so we let
    # coding_selection() load it lazily (cli.py passes its already-resolved
    # CLI_CONFIG instead, purely to avoid a redundant read).
    if not explicit:
        try:
            from agent.coding_context import coding_selection

            selection = coding_selection(platform="tui")
            if selection is not None:
                return selection
        except Exception:
            pass

    try:
        from toolsets import validate_toolset
    except Exception:
        validate_toolset = None

    if explicit and validate_toolset is not None:
        built_in = [name for name in explicit if validate_toolset(name)]
        unresolved = [name for name in explicit if name not in built_in]

        if unresolved:
            try:
                from hermes_cli.plugins import discover_plugins

                discover_plugins()
                plugin_valid = [name for name in unresolved if validate_toolset(name)]
            except Exception:
                plugin_valid = []

            if plugin_valid:
                built_in.extend(plugin_valid)
                unresolved = [name for name in unresolved if name not in plugin_valid]

        if any(name in {"all", "*"} for name in built_in):
            ignored = [name for name in explicit if name not in {"all", "*"}]
            if ignored:
                print(
                    "[tui] HERMES_TUI_TOOLSETS=all enables every toolset; "
                    f"ignoring additional entries: {', '.join(ignored)}",
                    file=sys.stderr,
                    flush=True,
                )
            return None

        if not unresolved:
            return built_in

        mcp_names: set[str] = set()
        mcp_disabled: set[str] = set()
        try:
            from hermes_cli.config import read_raw_config
            from hermes_cli.tools_config import _parse_enabled_flag

            raw_cfg = read_raw_config()
            mcp_servers = (
                raw_cfg.get("mcp_servers")
                if isinstance(raw_cfg.get("mcp_servers"), dict)
                else {}
            )
            for name, server_cfg in mcp_servers.items():
                if not isinstance(server_cfg, dict):
                    continue
                if _parse_enabled_flag(server_cfg.get("enabled", True), default=True):
                    mcp_names.add(str(name))
                else:
                    mcp_disabled.add(str(name))
        except Exception:
            mcp_names = set()
            mcp_disabled = set()

        mcp_valid = [name for name in unresolved if name in mcp_names]
        disabled = [name for name in unresolved if name in mcp_disabled]
        unknown = [
            name
            for name in unresolved
            if name not in mcp_names and name not in mcp_disabled
        ]
        valid = built_in + mcp_valid

        if unknown:
            print(
                f"[tui] ignoring unknown HERMES_TUI_TOOLSETS entries: {', '.join(unknown)}",
                file=sys.stderr,
                flush=True,
            )
        if disabled:
            print(
                "[tui] ignoring disabled MCP servers in HERMES_TUI_TOOLSETS "
                "(set enabled: true in config.yaml to use): "
                f"{', '.join(disabled)}",
                file=sys.stderr,
                flush=True,
            )

        if valid:
            return valid

        fallback_notice = (
            "[tui] no valid HERMES_TUI_TOOLSETS entries; using configured CLI toolsets"
        )

    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        cfg = cfg if cfg is not None else load_config()

        # Runtime toolset resolution must include default MCP servers so the
        # agent can actually call them. Passing ``False`` here is the
        # config-editing variant — used when we need to persist a toolset
        # list without baking in implicit MCP defaults. Using the wrong
        # variant at agent creation time makes MCP tools silently missing
        # from the TUI. See PR #3252 for the original design split.
        enabled = sorted(
            _get_platform_tools(cfg, "cli", include_default_mcp_servers=True)
        )
        if fallback_notice is not None:
            print(fallback_notice, file=sys.stderr, flush=True)
        return enabled or None
    except Exception:
        if fallback_notice is not None:
            print(
                "[tui] no valid HERMES_TUI_TOOLSETS entries and configured CLI toolsets could not be loaded; enabling all toolsets",
                file=sys.stderr,
                flush=True,
            )
        return None


def _load_disabled_toolsets() -> list[str] | None:
    raw = (_load_cfg().get("agent") or {}).get("disabled_toolsets") or []
    if isinstance(raw, str):
        values = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result or None


def _session_tool_progress_mode(sid: str) -> str:
    return str(_sessions.get(sid, {}).get("tool_progress_mode", "all") or "all")


def _session_verbose(sid: str) -> bool:
    return _session_tool_progress_mode(sid) == "verbose"


def _tool_progress_enabled(sid: str) -> bool:
    return _session_tool_progress_mode(sid) != "off"


def _restart_slash_worker(sid: str, session: dict):
    # sid is REQUIRED — callers already have it in scope; avoids an O(N)
    # reverse-lookup over _sessions and lets _attach_worker verify identity
    # against the canonical mapping. Signature matches upstream
    # tui_gateway/server.py::_restart_slash_worker(sid, session) after
    # absorption of bc4dbce858 (#50375 model-switch no-op fix), which started
    # passing sid through but left the dovie fork's older 1-arg signature in
    # place — surface error on /model: "takes 1 positional argument but 2
    # were given".
    worker = session.get("slash_worker")
    if worker:
        try:
            worker.close()
        except Exception:
            pass
    # C2: spawn outside the lock (subprocess start is slow), then re-check
    # via _attach_worker. If the session was torn down between the spawn and
    # attach, _attach_worker closes the orphan worker for us.
    try:
        new_worker = _SlashWorker(
            session["session_key"],
            getattr(session.get("agent"), "model", _resolve_model()),
        )
    except Exception:
        session["slash_worker"] = None
        return
    _attach_worker(sid, session, new_worker)


def _persist_model_switch(result) -> None:
    from hermes_cli.config import save_config

    cfg = _load_cfg()
    model_cfg = cfg.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
        cfg["model"] = model_cfg

    model_cfg["default"] = result.new_model
    model_cfg["provider"] = result.target_provider
    if result.base_url:
        model_cfg["base_url"] = result.base_url
    else:
        model_cfg.pop("base_url", None)
    save_config(cfg)


def _apply_model_switch(
    sid: str,
    session: dict,
    raw_input: str,
    *,
    confirm_expensive_model: bool = False,
    pin_session_override: bool = True,
    parsed_flags: tuple[str, str, bool, bool, bool] | None = None,
) -> dict:
    from hermes_cli.model_switch import (
        parse_model_flags,
        resolve_persist_behavior,
        switch_model,
    )
    from hermes_cli.runtime_provider import resolve_runtime_provider

    if parsed_flags is None:
        parsed_flags = parse_model_flags(raw_input)
    (
        model_input,
        explicit_provider,
        is_global_flag,
        _force_refresh,
        is_session,
    ) = parsed_flags
    persist_global = resolve_persist_behavior(is_global_flag, is_session)
    if not model_input:
        raise ValueError("model value required")

    agent = session.get("agent")
    if agent:
        current_provider = getattr(agent, "provider", "") or ""
        current_model = getattr(agent, "model", "") or ""
        current_base_url = getattr(agent, "base_url", "") or ""
        current_api_key = getattr(agent, "api_key", "") or ""
    else:
        runtime = resolve_runtime_provider(requested=None)
        current_provider = str(runtime.get("provider", "") or "")
        current_model = _resolve_model()
        current_base_url = str(runtime.get("base_url", "") or "")
        # Preserve a callable api_key (Azure Foundry Entra ID bearer
        # provider) unchanged — ``str(...)`` would produce
        # ``"<function ...>"`` and poison downstream switch_model
        # validation. Match the agent-present branch's behavior at the
        # top of this block.
        _runtime_key = runtime.get("api_key", "")
        if callable(_runtime_key) and not isinstance(_runtime_key, str):
            current_api_key = _runtime_key
        else:
            current_api_key = str(_runtime_key or "")

    # Load user-defined providers so switch_model can resolve named custom
    # endpoints (e.g. "ollama-launch") and validate against saved model lists.
    user_provs = None
    custom_provs = None
    try:
        from hermes_cli.config import get_compatible_custom_providers, load_config

        cfg = load_config()
        user_provs = cfg.get("providers")
        custom_provs = get_compatible_custom_providers(cfg)
    except Exception:
        pass

    result = switch_model(
        raw_input=model_input,
        current_provider=current_provider,
        current_model=current_model,
        current_base_url=current_base_url,
        current_api_key=current_api_key,
        is_global=persist_global,
        explicit_provider=explicit_provider,
        user_providers=user_provs,
        custom_providers=custom_provs,
    )
    if not result.success:
        raise ValueError(result.error_message or "model switch failed")

    if not confirm_expensive_model:
        try:
            from hermes_cli.model_cost_guard import expensive_model_warning

            warning = expensive_model_warning(
                result.new_model,
                provider=result.target_provider,
                base_url=result.base_url or current_base_url,
                api_key=result.api_key or current_api_key,
                model_info=result.model_info,
            )
        except Exception:
            warning = None
        if warning is not None:
            return {
                "value": result.new_model,
                "warning": warning.message,
                "confirm_required": True,
                "confirm_message": warning.message,
            }

    if agent:
        try:
            from hermes_cli.context_switch_guard import merge_preflight_compression_warning

            _cfg_ctx = None
            if isinstance(cfg, dict):
                _mc = cfg.get("model", {})
                if isinstance(_mc, dict) and _mc.get("context_length") is not None:
                    _cfg_ctx = int(_mc["context_length"])
            merge_preflight_compression_warning(
                result,
                agent=agent,
                messages=list(session.get("history", [])),
                custom_providers=custom_provs,
                config_context_length=_cfg_ctx,
            )
        except Exception as exc:
            logger.debug("preflight-compression switch warning failed: %s", exc)

    if not confirm_expensive_model:
        try:
            from hermes_cli.model_cost_guard import expensive_model_warning

            warning = expensive_model_warning(
                result.new_model,
                provider=result.target_provider,
                base_url=result.base_url or current_base_url,
                api_key=result.api_key or current_api_key,
                model_info=result.model_info,
            )
        except Exception:
            warning = None
        if warning is not None:
            confirm_msg = warning.message
            if result.warning_message:
                confirm_msg = f"{confirm_msg}\n\n{result.warning_message}"
            return {
                "value": result.new_model,
                "warning": confirm_msg,
                "confirm_required": True,
                "confirm_message": confirm_msg,
            }

    if agent:
        try:
            agent.switch_model(
                new_model=result.new_model,
                new_provider=result.target_provider,
                api_key=result.api_key,
                base_url=result.base_url,
                api_mode=result.api_mode,
            )
        except Exception as exc:
            # The in-place swap rolled the agent back to the old working
            # model/client and re-raised.  Abort the commit: do NOT restart the
            # slash worker, persist runtime, append the switch marker, set a
            # session model_override, or persist to config — all of which would
            # otherwise leave the session pinned to a broken model and kill the
            # conversation on the next turn (#50163).  A failed switch is a
            # no-op; surface a clean error to the client.
            logger.warning("In-place model switch failed for TUI agent: %s", exc)
            raise ValueError(
                f"Model switch to {result.new_model} failed ({exc}); "
                f"staying on {getattr(agent, 'model', current_model)}."
            ) from exc
        _restart_slash_worker(sid, session)
        _persist_live_session_runtime(session)
        _persist_live_session_system_prompt(session)
        _append_model_switch_marker(
            session, model=result.new_model, provider=result.target_provider
        )
        _emit("session.info", sid, _session_info(agent, session))

    os.environ["HERMES_MODEL"] = result.new_model
    os.environ["HERMES_INFERENCE_MODEL"] = result.new_model
    # Keep the process-level provider env vars in sync with the user's
    # explicit choice so any ambient re-resolution (credential pool refresh,
    # compressor rebuild, aux clients) and startup re-resolution on /new
    # both pick up the new provider instead of the original one persisted
    # in config or env.
    #
    # HERMES_TUI_PROVIDER is the canonical "explicit-this-process" carrier
    # consumed by _resolve_startup_runtime() — set it unconditionally on
    # /model so /new can't fall through to static-catalog detection and
    # pick a coincidentally-matching native provider (fixes #16857).
    if result.target_provider:
        os.environ["HERMES_INFERENCE_PROVIDER"] = result.target_provider
        os.environ["HERMES_TUI_PROVIDER"] = result.target_provider
    if persist_global:
        _persist_model_switch(result)
    return {
        "value": result.new_model,
        "warning": result.warning_message or "",
        "confirm_required": False,
    }


def _compress_session_history(
    session: dict,
    focus_topic: str | None = None,
    approx_tokens: int | None = None,
    before_messages: list | None = None,
    history_version: int | None = None,
) -> tuple[int, dict]:
    from agent.model_metadata import estimate_request_tokens_rough

    agent = session["agent"]
    # Snapshot history under the lock so the LLM-bound compression call
    # below does NOT hold history_lock for the duration of the request —
    # otherwise other handlers acquiring the lock (prompt.submit etc.)
    # block on the dispatcher loop while compaction runs.
    if before_messages is None or history_version is None:
        with session["history_lock"]:
            before_messages = list(session.get("history", []))
            history_version = int(session.get("history_version", 0))
    history = before_messages
    if len(history) < 4:
        usage = _get_usage(agent)
        return 0, usage
    if approx_tokens is None:
        # Include system prompt + tool schemas so the figure reflects real
        # request pressure, not a transcript-only underestimate (#6217).
        _sys_prompt = getattr(agent, "_cached_system_prompt", "") or ""
        _tools = getattr(agent, "tools", None) or None
        approx_tokens = estimate_request_tokens_rough(
            history, system_prompt=_sys_prompt, tools=_tools
        )
    # Pass system_message=None so AIAgent._compress_context rebuilds the
    # system prompt cleanly via _build_system_prompt(None). Passing the
    # cached prompt (which already contains the agent identity block)
    # makes the rebuild append the identity a second time. Mirrors the
    # CLI's _manual_compress fix for issue #15281.
    compressed, _ = agent._compress_context(
        history,
        None,
        approx_tokens=approx_tokens,
        focus_topic=focus_topic or None,
    )
    with session["history_lock"]:
        if int(session.get("history_version", 0)) != history_version:
            # External mutation during compaction — drop the compressed
            # result so we don't clobber concurrent edits.
            usage = _get_usage(agent)
            return 0, usage
        session["history"] = compressed
        session["history_version"] = history_version + 1
    usage = _get_usage(agent)
    return len(history) - len(compressed), usage


def _sync_session_key_after_compress(
    sid: str,
    session: dict,
    *,
    clear_pending_title: bool = True,
    restart_slash_worker: bool = True,
) -> None:
    """Re-anchor session_key when AIAgent._compress_context rotates session_id.

    AIAgent._compress_context ends the current SessionDB session and creates
    a new continuation session, rotating ``agent.session_id``.  The TUI
    gateway keeps the gateway-side ``session_key`` separate (used for
    approval routing, slash worker init, DB title/history lookups, yolo
    state).  Without this sync, those operations would target the ended
    parent session while the agent writes to the new continuation session.

    Policy flags:
        clear_pending_title: True for manual /compress (title belongs to old
            session). False for post-turn auto-compression (preserve user
            intent so pending_title can be applied to the continuation).
        restart_slash_worker: True for manual /compress and post-turn
            auto-compression (worker holds stale session key). False only
            if the caller manages the worker lifecycle separately.
    """
    agent = session.get("agent")
    new_session_id = getattr(agent, "session_id", None) or ""
    old_key = session.get("session_key", "") or ""
    if not new_session_id or new_session_id == old_key:
        return

    try:
        from tools.approval import (
            disable_session_yolo,
            enable_session_yolo,
            is_session_yolo_enabled,
            register_gateway_notify,
            unregister_gateway_notify,
        )

        try:
            unregister_gateway_notify(old_key)
        except Exception:
            pass
        session["session_key"] = new_session_id
        try:
            yolo_was_on = is_session_yolo_enabled(old_key)
        except Exception:
            yolo_was_on = False
        if yolo_was_on:
            try:
                enable_session_yolo(new_session_id)
                disable_session_yolo(old_key)
            except Exception:
                pass
        try:
            register_gateway_notify(
                new_session_id,
                lambda data: _emit("approval.request", sid, data),
            )
        except Exception:
            pass
    except Exception:
        # Even if the approval module fails to import, still anchor the
        # session_key on the new continuation id so downstream lookups
        # don't keep targeting the ended row.
        session["session_key"] = new_session_id

    if clear_pending_title:
        session["pending_title"] = None
    if restart_slash_worker:
        try:
            _restart_slash_worker(sid, session)
        except Exception:
            pass


def _current_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


_TUI_VERBOSE_TEXT_MAX_CHARS = 16_000
_TUI_VERBOSE_TEXT_MAX_LINES = 240


def _cap_tui_verbose_text(text: str) -> str:
    if (
        len(text) <= _TUI_VERBOSE_TEXT_MAX_CHARS
        and text.count("\n") < _TUI_VERBOSE_TEXT_MAX_LINES
    ):
        return text

    idx = len(text)
    start = 0
    for _ in range(_TUI_VERBOSE_TEXT_MAX_LINES):
        idx = text.rfind("\n", 0, idx)
        if idx < 0:
            start = 0
            break
        start = idx + 1

    line_start = start
    start = max(line_start, len(text) - _TUI_VERBOSE_TEXT_MAX_CHARS)
    if start > line_start:
        next_break = text.find("\n", start)
        if 0 <= next_break < len(text) - 1:
            start = next_break + 1

    tail = text[start:].lstrip()
    omitted_chars = max(0, len(text) - len(tail))
    omitted_lines = text[:start].count("\n")
    if omitted_lines:
        label = (
            "[showing verbose tail; omitted "
            f"{omitted_lines} lines / {omitted_chars} chars]\n"
        )
    else:
        label = f"[showing verbose tail; omitted {omitted_chars} chars]\n"
    return f"{label}{tail}"


def _redact_tui_verbose_text(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        redacted = redact_sensitive_text(str(text), force=True)
    except Exception:
        return ""
    return _cap_tui_verbose_text(redacted)


def _tool_args_text(args: dict) -> str:
    try:
        raw = json.dumps(args or {}, indent=2, ensure_ascii=False, default=str)
    except Exception:
        raw = str(args or {})
    return _redact_tui_verbose_text(raw)


def _tool_result_text(result: object) -> str:
    try:
        from agent.tool_dispatch_helpers import _multimodal_text_summary

        raw = _multimodal_text_summary(result)
    except Exception:
        raw = str(result)
    return _redact_tui_verbose_text(raw)


def _tool_event_bridge() -> GatewayToolEventBridge:
    return GatewayToolEventBridge(
        sessions=_sessions,
        emit=_emit,
        tool_progress_enabled=_tool_progress_enabled,
        session_cwd=_session_cwd,
        session_verbose=_session_verbose,
        tool_args_text=_tool_args_text,
        tool_result_text=_tool_result_text,
        before_tool_boundary=_before_tool_text_boundary,
        thinking_event="thinking.delta",
    )


def _before_tool_text_boundary(sid: str, event_type: str) -> None:
    session = _sessions.get(sid)
    if not isinstance(session, dict):
        return
    callback = session.get("stream_text_boundary_callback")
    if callable(callback):
        callback(event_type)


def _on_tool_start(sid: str, tool_call_id: str, name: str, args: dict) -> None:
    _tool_event_bridge().on_tool_start(sid, tool_call_id, name, args)


def _on_tool_complete(sid: str, tool_call_id: str, name: str, args: dict, result: str) -> None:
    _tool_event_bridge().on_tool_complete(sid, tool_call_id, name, args, result)


def _agent_cbs(sid: str) -> dict:
    return _tool_event_bridge().agent_callbacks(
        sid,
        block=_block,
        status_update=_status_update,
    )


def _wire_callbacks(sid: str):
    wire_secret_callbacks(sid, block=_block)


def _render_personality_prompt(value) -> str:
    if isinstance(value, dict):
        parts = [value.get("system_prompt", "")]
        if value.get("tone"):
            parts.append(f'Tone: {value["tone"]}')
        if value.get("style"):
            parts.append(f'Style: {value["style"]}')
        return "\n".join(p for p in parts if p)
    return str(value)


def _available_personalities(cfg: dict | None = None) -> dict:
    try:
        from cli import load_cli_config

        return (load_cli_config().get("agent") or {}).get("personalities", {}) or {}
    except Exception:
        try:
            from hermes_cli.config import load_config as _load_full_cfg

            return (_load_full_cfg().get("agent") or {}).get("personalities", {}) or {}
        except Exception:
            cfg = cfg or _load_cfg()
            return (cfg.get("agent") or {}).get("personalities", {}) or {}


def _validate_personality(value: str, cfg: dict | None = None) -> tuple[str, str]:
    raw = str(value or "").strip()
    name = raw.lower()
    if not name or name in {"none", "default", "neutral"}:
        return "", ""

    personalities = _available_personalities(cfg)
    if name not in personalities:
        names = sorted(personalities)
        available = ", ".join(f"`{n}`" for n in names)
        base = f"Unknown personality: `{raw}`."
        if available:
            base += f"\n\nAvailable: `none`, {available}"
        else:
            base += "\n\nNo personalities configured."
        raise ValueError(base)

    return name, _render_personality_prompt(personalities[name])


def _apply_personality_to_session(
    sid: str, session: dict, new_prompt: str
) -> tuple[bool, dict | None]:
    """Apply a personality change to an existing session without resetting history.

    Updates the agent's ephemeral system prompt in-place so the new personality
    takes effect on the next turn.  The cached base system prompt is left intact
    (ephemeral_system_prompt is appended at API-call time, not baked into the
    cache), which preserves prompt-cache hits.

    Also injects a system-role marker into the conversation history so the model
    knows to pivot its style from this point forward (without this, LLMs tend to
    continue the tone established by earlier messages in the transcript).

    Returns (history_reset, info) — history_reset is always False since we
    preserve the conversation.
    """
    if not session:
        return False, None

    agent = session.get("agent")
    if agent:
        agent.ephemeral_system_prompt = new_prompt or None
        # Inject a pivot marker into history so the model sees the change point.
        # This prevents it from pattern-matching its prior style.
        if new_prompt:
            marker = (
                "[System: The user has changed the assistant's personality. "
                "From this point forward, adopt the following persona and respond "
                f"accordingly: {new_prompt}]"
            )
        else:
            marker = (
                "[System: The user has cleared the personality overlay. "
                "From this point forward, respond in your normal default style.]"
            )
        with session["history_lock"]:
            session["history"].append({"role": "user", "content": marker})
            session["history_version"] = int(session.get("history_version", 0)) + 1
        info = _session_info(agent, session)
        _emit("session.info", sid, info)
        return False, info
    return False, None


def _cfg_max_turns(cfg: dict, default: int) -> int:
    try:
        env_max = int(os.environ.get("HERMES_TUI_MAX_TURNS", "") or 0)
        if env_max > 0:
            return env_max
    except (TypeError, ValueError):
        pass
    agent_cfg = cfg.get("agent") or {}
    return int(agent_cfg.get("max_turns") or cfg.get("max_turns") or default)


def _parse_tui_skills_env() -> list[str]:
    raw = os.environ.get("HERMES_TUI_SKILLS", "")
    skills: list[str] = []
    seen: set[str] = set()
    for part in raw.replace("\n", ",").split(","):
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            skills.append(item)
    return skills


def _background_agent_kwargs(agent, task_id: str) -> dict:
    cfg = _load_cfg()

    return {
        "base_url": getattr(agent, "base_url", None) or None,
        "api_key": getattr(agent, "api_key", None) or None,
        "provider": getattr(agent, "provider", None) or None,
        "api_mode": getattr(agent, "api_mode", None) or None,
        "acp_command": getattr(agent, "acp_command", None) or None,
        "acp_args": getattr(agent, "acp_args", None) or None,
        "model": getattr(agent, "model", None) or _resolve_model(),
        "max_iterations": _cfg_max_turns(cfg, 25),
        "enabled_toolsets": getattr(agent, "enabled_toolsets", None)
        or _load_enabled_toolsets(),
        "quiet_mode": True,
        "verbose_logging": False,
        "ephemeral_system_prompt": getattr(agent, "ephemeral_system_prompt", None)
        or None,
        "providers_allowed": getattr(agent, "providers_allowed", None),
        "providers_ignored": getattr(agent, "providers_ignored", None),
        "providers_order": getattr(agent, "providers_order", None),
        "provider_sort": getattr(agent, "provider_sort", None),
        "provider_require_parameters": getattr(
            agent, "provider_require_parameters", False
        ),
        "provider_data_collection": getattr(agent, "provider_data_collection", None),
        "openrouter_min_coding_score": getattr(agent, "openrouter_min_coding_score", None),
        "session_id": task_id,
        "reasoning_config": getattr(agent, "reasoning_config", None)
        or _load_reasoning_config(),
        "service_tier": getattr(agent, "service_tier", None) or _load_service_tier(),
        "request_overrides": dict(getattr(agent, "request_overrides", {}) or {}),
        "platform": "tui",
        "session_db": _get_db(),
        "fallback_model": getattr(agent, "_fallback_model", None),
    }


def _reset_session_agent(sid: str, session: dict) -> dict:
    tokens = _set_session_context(session["session_key"])
    try:
        new_agent = _make_agent(
            sid, session["session_key"], session_id=session["session_key"]
        )
    finally:
        _clear_session_context(tokens)
    session["agent"] = new_agent
    session["attached_images"] = []
    session["edit_snapshots"] = {}
    session["image_counter"] = 0
    session["running"] = False
    session["show_reasoning"] = _load_show_reasoning()
    session["tool_progress_mode"] = _load_tool_progress_mode()
    session["tool_started_at"] = {}
    with session["history_lock"]:
        session["history"] = []
        session["history_version"] = int(session.get("history_version", 0)) + 1
    info = _session_info(new_agent)
    _emit("session.info", sid, info)
    _restart_slash_worker(sid, session)
    return info


def _schedule_mcp_late_refresh(sid: str, agent) -> None:
    """Refresh a session's tool snapshot when MCP discovery lands late.

    The agent snapshots ``agent.tools`` once at build time and never re-reads
    the registry (run_agent/agent_init). ``_make_agent`` briefly joins the
    background MCP discovery thread (``wait_for_mcp_discovery``, ~0.75s) so
    already-spawning servers land in that snapshot — but a server that takes
    longer than the bound to connect (common for an HTTP MCP server on first
    connect) lands *after* the agent is built. Its tools are then absent from
    both the agent and the banner for the whole session, even though the
    classic CLI shows them (the CLI re-derives ``get_tool_definitions`` at
    banner render time, which re-waits, so it picks them up).

    This schedules an off-critical-path daemon that waits for discovery to
    finish, then rebuilds the snapshot and re-emits ``session.info`` so both
    the agent's callable tools and the banner count catch up — the same
    rebuild ``/reload-mcp`` performs, but automatic.

    Cache safety: the rebuild only runs while the session is still pre-first-
    turn (no API call made yet → nothing cached to invalidate). If the user
    has already sent a message, we leave the snapshot frozen rather than
    invalidate the prompt cache mid-conversation — those late tools then
    require an explicit ``/reload-mcp`` (which gates on user consent), exactly
    as today. No-op when discovery already finished before the agent build.
    """
    try:
        from tui_gateway.entry import mcp_discovery_in_flight, join_mcp_discovery
    except Exception:
        return
    if not mcp_discovery_in_flight():
        return

    def _wait_then_refresh() -> None:
        # Bounded but generous — a server still not connected after this is
        # genuinely slow/dead; the user can /reload-mcp once it recovers.
        if not join_mcp_discovery(timeout=30.0):
            return
        with _sessions_lock:
            session = _sessions.get(sid)
            # Session may have been closed/reset while we waited.
            if session is None or session.get("agent") is not agent:
                return
            # Cache safety: never rebuild the tool list once the conversation
            # has started — that would invalidate the cached prompt prefix.
            if (
                int(getattr(agent, "_user_turn_count", 0) or 0) > 0
                or int(getattr(agent, "_api_call_count", 0) or 0) > 0
            ):
                return
            try:
                from tools.mcp_tool import refresh_agent_mcp_tools

                added = refresh_agent_mcp_tools(agent, quiet_mode=True)
            except Exception as exc:
                logger.warning(
                    "Late MCP refresh: tool snapshot rebuild failed for %s: %s",
                    sid,
                    exc,
                )
                return
            # No new tools landed (discovery added nothing) → don't churn the client.
            if not added:
                return
            info = _session_info(agent, session)
        # Emit outside the lock — write_json must not block under _sessions_lock.
        _emit("session.info", sid, info)

    threading.Thread(
        target=_wait_then_refresh,
        name=f"tui-mcp-late-refresh-{sid}",
        daemon=True,
    ).start()


def _make_agent(
    sid: str,
    key: str,
    session_id: str | None = None,
    cwd: str | None = None,
    agent_context_mode: str | None = None,
):
    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from tui_gateway.services.runtime_credentials import remember_requested_runtime_provider
    from tui_gateway.services.toolset_scope import resolve_session_toolsets

    cfg = _load_cfg()
    agent_cfg = cfg.get("agent") or {}
    system_prompt = (agent_cfg.get("system_prompt", "") or "").strip()
    startup_skills = _parse_tui_skills_env()
    if startup_skills:
        from agent.skill_commands import build_preloaded_skills_prompt

        skills_prompt, _loaded_skills, missing_skills = build_preloaded_skills_prompt(
            startup_skills,
            task_id=session_id or key,
        )
        if missing_skills:
            raise ValueError(f"Unknown skill(s): {', '.join(missing_skills)}")
        if skills_prompt:
            system_prompt = "\n\n".join(
                part for part in (system_prompt, skills_prompt) if part
            ).strip()
    model, requested_provider = _resolve_startup_runtime()
    runtime = resolve_runtime_provider(
        requested=requested_provider,
        target_model=model or None,
    )
    enabled_toolsets, disabled_toolsets = resolve_session_toolsets(
        session=_sessions.get(sid),
        session_id=session_id or key,
        load_enabled_toolsets=_load_enabled_toolsets,
        load_disabled_toolsets=_load_disabled_toolsets,
    )
    session_context = dict(_sessions.get(sid) or {})
    if agent_context_mode:
        session_context["agent_context_mode"] = agent_context_mode
    context_options = _agent_context_options_for_session(session_context)
    agent = AIAgent(
        model=model,
        max_iterations=_cfg_max_turns(cfg, 90),
        provider=runtime.get("provider"),
        base_url=runtime.get("base_url"),
        api_key=runtime.get("api_key"),
        api_mode=runtime.get("api_mode"),
        acp_command=runtime.get("command"),
        acp_args=runtime.get("args"),
        credential_pool=runtime.get("credential_pool"),
        quiet_mode=True,
        verbose_logging=_load_tool_progress_mode() == "verbose",
        reasoning_config=_load_reasoning_config(),
        service_tier=_load_service_tier(),
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        platform="tui",
        session_id=session_id or key,
        session_db=_db_for_stable_session(session_id or key),
        ephemeral_system_prompt=system_prompt or None,
        cwd=cwd,
        checkpoints_enabled=is_truthy_value(os.environ.get("HERMES_TUI_CHECKPOINTS")),
        pass_session_id=is_truthy_value(os.environ.get("HERMES_TUI_PASS_SESSION_ID")),
        **context_options,
        **_agent_cbs(sid),
    )
    if cwd:
        agent.session_cwd = cwd
    remember_requested_runtime_provider(agent, runtime, requested_provider)
    return agent


def _init_session(
    sid: str,
    key: str,
    agent,
    history: list,
    cols: int = 80,
    cwd: str | None = None,
    workspace: dict | None = None,
    profile_context: dict | None = None,
    agent_context_mode: str | None = None,
):
    session_record = {
        "agent": agent,
        "session_key": key,
        "cwd": cwd or getattr(agent, "session_cwd", ""),
        "workspace": dict(workspace or {}),
        "profile_context": profile_context,
        "agent_context_mode": _agent_context_mode_from_params({"agent_context_mode": agent_context_mode}),
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": cols,
        "slash_worker": None,
        "show_reasoning": _load_show_reasoning(),
        "tool_progress_mode": _load_tool_progress_mode(),
        "edit_snapshots": {},
        "tool_started_at": {},
        # Pin async event emissions to whichever transport created the
        # session (stdio for Ink, JSON-RPC WS for the dashboard sidebar).
        "transport": current_transport() or _stdio_transport,
    }
    with _sessions_lock:
        _sessions[sid] = session_record
    try:
        slash_worker = _SlashWorker(
            key, getattr(agent, "model", _resolve_model())
        )
        # C2: stricter than the previous `sid in _sessions` check — uses
        # identity comparison so a same-sid replacement (close+recreate
        # under the same sid) doesn't accidentally inherit this worker.
        _attach_worker(sid, session_record, slash_worker)
    except Exception:
        # Defer hard-failure to slash.exec; chat still works without slash worker.
        with _sessions_lock:
            if _sessions.get(sid) is session_record:
                session_record["slash_worker"] = None
    try:
        from tools.approval import register_gateway_notify, load_permanent_allowlist

        register_gateway_notify(key, lambda data: _emit("approval.request", sid, data))
        load_permanent_allowlist()
    except Exception:
        pass
    # Surface the self-improvement background review's "💾 …" summary as a
    # review.summary event so Ink can render it as a persistent system line
    # in the transcript. In the CLI path this message is printed via
    # prompt_toolkit; the TUI has no equivalent print surface, so without
    # this callback the review would write the skill/memory change silently.
    try:
        agent.background_review_callback = lambda message, _sid=sid: _emit(
            "review.summary", _sid, {"text": str(message)}
        )
    except Exception:
        # Bare AIAgents that don't expose the attribute (unlikely, but keep
        # session startup resilient).
        pass
    _wire_callbacks(sid)
    with _sessions_lock:
        session = _sessions.get(sid)
        if session is not None:
            session["_notif_stop"] = _start_notification_poller(sid, session)
    _notify_session_boundary("on_session_reset", key)
    with _sessions_lock:
        session = _sessions.get(sid, {})
    _emit("session.info", sid, _session_info(agent, session))


def _new_session_key() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _resolve_runtime_session(target: str) -> tuple[str, dict | None]:
    requested = str(target or "").strip()
    if not requested:
        return "", None

    with _sessions_lock:
        session = _sessions.get(requested)
    if session is not None:
        return requested, session

    try:
        with _sessions_lock:
            snapshot = [
                (runtime_sid, candidate)
                for runtime_sid, candidate in _sessions.items()
                if str((candidate or {}).get("session_key") or "") == requested
            ]
    except Exception:
        snapshot = []
    if snapshot:
        snapshot.sort(
            key=lambda item: (
                bool((item[1] or {}).get("running")),
                float((item[1] or {}).get("run_updated_at") or 0),
            ),
            reverse=True,
        )
        return snapshot[0]
    return "", None


def _session_run_snapshot(runtime_sid: str, session: dict | None, db=None) -> dict:
    from tui_gateway.services import run_control

    session = session or {}
    stable_session_id = str(session.get("session_key") or runtime_sid or "")
    control_state = run_control.session_status(
        stable_session_id,
        db=db,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    )
    running = bool(session.get("running") or control_state.get("running"))
    return {
        "session_id": runtime_sid or "",
        "stored_session_id": stable_session_id,
        "running": running,
        "runtime_scope_key": str(
            control_state.get("runtime_scope_key")
            or session.get("active_runtime_scope_key")
            or stable_session_id
        ),
        "active_runtime_session_id": runtime_sid or "",
        "active_run_id": str(
            session.get("active_run_id") or control_state.get("active_run_id") or ""
        ) if running else "",
        "active_turn_id": str(
            session.get("active_turn_id") or control_state.get("active_turn_id") or ""
        ) if running else "",
        "run_started_at": (
            session.get("run_started_at") or control_state.get("run_started_at") or 0
        ) if running else 0,
        "run_updated_at": (
            session.get("run_updated_at") or control_state.get("run_updated_at") or 0
        ) if running else 0,
        "last_event_seq": int(control_state.get("last_event_seq") or 0),
    }


def _with_checkpoints(session, fn):
    cwd = (
        session.get("cwd")
        or getattr(session.get("agent"), "session_cwd", "")
        or os.getenv("DOVIE_WORKSPACE_ROOT", "")
        or os.getenv("TERMINAL_CWD", "")
    )
    if not cwd and os.getenv("DOVIE_PROCESS_ROLE") != "hermes-worker":
        cwd = os.getcwd()
    return fn(session["agent"]._checkpoint_mgr, cwd)


def _resolve_checkpoint_hash(mgr, cwd: str, ref: str) -> str:
    try:
        checkpoints = mgr.list_checkpoints(cwd)
        idx = int(ref) - 1
    except ValueError:
        return ref
    if 0 <= idx < len(checkpoints):
        return checkpoints[idx].get("hash", ref)
    raise ValueError(f"Invalid checkpoint number. Use 1-{len(checkpoints)}.")


# ── Methods: session ─────────────────────────────────────────────────


@method("session.create")
def _(rid, params: dict) -> dict:
    sid = uuid.uuid4().hex[:8]
    key = _new_session_key()
    cols = int(params.get("cols", 80))
    history = _coerce_seed_history(params.get("messages"))
    title = str(params.get("title") or "").strip()
    # Did the client pick a workspace, or are we falling back to the gateway's
    # launch directory? Only an explicit choice is persisted as the session's
    # workspace (see _ensure_session_db_row); otherwise it lands in "No
    # workspace" instead of whatever folder the desktop launched in.
    raw_cwd = str(params.get("cwd") or "").strip()
    try:
        explicit_cwd = bool(raw_cwd) and os.path.isdir(os.path.abspath(os.path.expanduser(raw_cwd)))
    except Exception:
        explicit_cwd = False
    resolved_cwd = _completion_cwd(params)
    _enable_gateway_prompts()

    # ``profile`` (app-global remote mode): a new chat started under a non-launch
    # profile must build its agent + persist against THAT profile's home/state.db,
    # not the dashboard's launch profile. Stored on the session so _start_agent_build
    # and each turn re-bind HERMES_HOME. None/own profile → launch (unchanged).
    profile = (params.get("profile") or "").strip() or None
    profile_home = _profile_home(profile)

    # The desktop composer owns its model/effort/fast as plain UI state and ships
    # it on every session.create. Honor each as a PER-SESSION override (built into
    # the agent below) — never a global config write, so picking a model/effort
    # for a new chat can't mutate the profile default. provider is optional
    # (resolved at build).
    create_model = str(params.get("model") or "").strip()
    session_model_override = (
        {"model": create_model, "provider": str(params.get("provider") or "").strip() or None}
        if create_model
        else None
    )
    create_reasoning_override = None
    if effort := str(params.get("reasoning_effort") or "").strip():
        try:
            from hermes_constants import parse_reasoning_effort

            create_reasoning_override = parse_reasoning_effort(effort)
        except Exception:
            create_reasoning_override = None
    # Only pin "fast" when explicitly requested; leaving it None lets the build
    # fall back to the profile default service tier rather than forcing normal.
    create_service_tier_override = "priority" if params.get("fast") else None

    ready = threading.Event()
    now = time.time()
    lease, limit_message = _claim_active_session_slot(key, live_session_id=sid)
    if limit_message is not None:
        return _err(rid, 4090, limit_message)

    with _sessions_lock:
        _sessions[sid] = {
            "agent": None,
            "agent_error": None,
            "agent_ready": ready,
            "attached_images": [],
            "close_on_disconnect": is_truthy_value(params.get("close_on_disconnect", False)),
            "active_session_lease": lease,
            "cols": cols,
            "created_at": now,
            "edit_snapshots": {},
            "explicit_cwd": explicit_cwd,
            "history": history,
            "history_lock": threading.Lock(),
            "history_version": 0,
            "image_counter": 0,
            "cwd": resolved_cwd,
            "inflight_turn": None,
            "last_active": now,
            "model_override": session_model_override,
            "create_reasoning_override": create_reasoning_override,
            "create_service_tier_override": create_service_tier_override,
            "pending_title": title or None,
            "profile_home": str(profile_home) if profile_home is not None else None,
            "running": False,
            "session_key": key,
            "show_reasoning": _load_show_reasoning(),
            "slash_worker": None,
            "tool_progress_mode": _load_tool_progress_mode(),
            "tool_started_at": {},
            "transport": current_transport() or _stdio_transport,
        }
        _register_session_cwd(_sessions[sid])
    # NOTE: we intentionally do NOT persist a DB row here. Every TUI/desktop
    # launch (and every "New agent" / draft) opens a session here just to paint
    # the composer, so eagerly creating a row left an "Untitled" empty session
    # behind for every launch the user never typed into. The row is now created
    # lazily on the first prompt (see _ensure_session_db_row + prompt.submit),
    # and the AIAgent's own INSERT-OR-IGNORE persists it on the first turn too.

    # Return the lightweight session immediately so Ink can paint the composer
    # + skeleton panel, then build the real AIAgent just after this response is
    # flushed.  This keeps startup responsive while still hydrating tools/skills
    # without requiring the user to submit a first prompt.
    def _deferred_build() -> None:
        session = _sessions.get(sid)
        if session is not None:
            _start_agent_build(sid, session)

    build_timer = threading.Timer(0.05, _deferred_build)
    build_timer.daemon = True
    build_timer.start()

    return _ok(
        rid,
        {
            "session_id": sid,
            "stored_session_id": key,
            "message_count": len(history),
            "messages": _history_to_messages(history),
            "info": {
                # Reflect the per-session model override (desktop composer pick)
                # in the immediate response so the client doesn't briefly clobber
                # its sticky pick with the global default before the deferred
                # build's session.info lands.
                "model": (
                    session_model_override.get("model")
                    if session_model_override
                    else _resolve_model()
                ),
                **(
                    {"provider": session_model_override["provider"]}
                    if session_model_override and session_model_override.get("provider")
                    else {}
                ),
                "tools": {},
                "skills": {},
                "cwd": _sessions[sid]["cwd"],
                "branch": _git_branch_for_cwd(_sessions[sid]["cwd"]),
                "lazy": True,
                "desktop_contract": DESKTOP_BACKEND_CONTRACT,
                "profile_name": _current_profile_name(),
            },
        },
    )


@method("session.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5006)
    try:
        # Resume picker should surface human conversation sessions from every
        # user-facing surface — CLI, TUI, all gateway platforms (including new
        # ones not enumerated here), ACP adapter clients, webhook sessions,
        # custom `HERMES_SESSION_SOURCE` values, and older installs with
        # different source labels. We deny-list only the noisy internal
        # sources (``tool`` sub-agent runs) rather than allow-listing a
        # fixed set of platform names that goes stale whenever a new
        # platform is added or a user names their own source.
        deny = frozenset({"tool"})

        limit = int(params.get("limit", 200) or 200)
        # Over-fetch modestly so per-source filtering doesn't leave us
        # short; the compression-tip projection in ``list_sessions_rich``
        # can also merge rows.
        fetch_limit = max(limit * 2, 200)
        rows = [
            s
            for s in db.list_sessions_rich(source=None, limit=fetch_limit)
            if (s.get("source") or "").strip().lower() not in deny
        ][:limit]
        return _ok(
            rid,
            {
                "sessions": [
                    {
                        "id": s["id"],
                        "title": s.get("title") or "",
                        "preview": s.get("preview") or "",
                        "started_at": s.get("started_at") or 0,
                        "message_count": s.get("message_count") or 0,
                        "source": s.get("source") or "",
                    }
                    for s in rows
                ]
            },
        )
    except Exception as e:
        return _err(rid, 5006, str(e))


@method("session.most_recent")
def _(rid, params: dict) -> dict:
    """Return the most recent human-facing session id, or ``None``.

    Mirrors ``session.list``'s deny-list behaviour (drops ``tool``
    sub-agent rows).  Used by TUI auto-resume when
    ``display.tui_auto_resume_recent`` is on; the field is also handy
    for any CLI tooling that wants "latest session" without paginating
    the full list.

    Contract: a ``{"session_id": null}`` result means "no eligible
    session found right now".  Errors are also folded into that
    null-result shape (and logged) so callers don't have to special-
    case JSON-RPC error envelopes for what is a normal "no answer".
    """
    db = _get_db()
    if db is None:
        return _ok(rid, {"session_id": None})
    try:
        deny = frozenset({"tool"})
        # Over-fetch by a generous bounded amount so heavy sub-agent
        # users (lots of recent ``tool`` rows) don't get a false
        # "no eligible session" answer.  ``session.list`` uses a
        # similar over-fetch strategy.
        rows = db.list_sessions_rich(source=None, limit=200)
        for row in rows:
            src = (row.get("source") or "").strip().lower()
            if src in deny:
                continue
            return _ok(
                rid,
                {
                    "session_id": row.get("id"),
                    "title": row.get("title") or "",
                    "started_at": row.get("started_at") or 0,
                    "source": row.get("source") or "",
                },
            )
        return _ok(rid, {"session_id": None})
    except Exception:
        logger.exception("session.most_recent failed")
        return _ok(rid, {"session_id": None})


@method("session.resume")
def _(rid, params: dict) -> dict:
    target = params.get("session_id", "")
    if not target:
        return _err(rid, 4006, "session_id required")
    try:
        cols = int(params.get("cols", 80))
    except (TypeError, ValueError):
        cols = 80
    # ``profile`` (app-global remote mode): resume a session that lives in another
    # local profile's state.db. None/own profile → the launch profile (unchanged).
    profile = (params.get("profile") or "").strip() or None
    profile_home = _profile_home(profile)

    # In a profile scope, the agent OWNS a long-lived db handle bound to that
    # profile (do NOT auto-close it here). Otherwise reuse the shared launch db.
    if profile_home is not None:
        from hermes_state import SessionDB

        db = SessionDB(db_path=profile_home / "state.db")
    else:
        db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5000)

    found = db.get_session(target)
    if not found:
        found = db.get_session_by_title(target)
        if found:
            target = found["id"]
        elif is_truthy_value(params.get("lazy", False)) and _child_run_active(target):
            # Race: a watch window opened on a freshly-spawned subagent. The
            # child relays `subagent.start` (which carries child_session_id and
            # triggers the window) BEFORE its first run_conversation() flushes
            # the DB row via _ensure_db_session, so db.get_session(target) is
            # momentarily empty. On slower hosts (notably WSL2, where SQLite +
            # process scheduling widen the gap) the window's resume consistently
            # lands inside this window and used to hard-fail "session not found"
            # — the frontend then 404'd on the REST messages fallback and the
            # window spun forever. The child is provably live (_child_run_active),
            # so proceed into the lazy branch with empty history; the live mirror
            # streams the whole turn anyway and the row exists by upgrade time.
            found = {}
        else:
            return _err(rid, 4007, "session not found")

    # Follow the compression-continuation chain to the live tip so a resume on
    # a rotated-out parent id binds to the descendant that actually holds the
    # post-compression turns. Auto-compression ends the session and forks a
    # continuation child; without this, resuming the original id (the desktop's
    # routed id when the chat was opened before it rotated) reloads the parent
    # transcript and the response generated after compression is missing — the
    # "I came back and the reply isn't there" bug on large sessions. Resolving
    # here also re-anchors the fast path below so a still-live rotated session
    # is reused (by its new key) instead of rebuilding a duplicate agent on the
    # stale parent. Skipped for lazy watch windows, which intentionally attach
    # to the exact child branch they were opened on.
    if found and not is_truthy_value(params.get("lazy", False)):
        try:
            tip = db.resolve_resume_session_id(target)
        except Exception:
            tip = target
        if tip and tip != target:
            target = tip
            found = db.get_session(target) or found

    profile_resume_cwd = str(found.get("cwd") or "").strip() or _profile_configured_cwd(
        profile_home
    )

    def _reuse_live_payload(sid: str, session: dict) -> dict:
        payload = _live_session_payload(
            sid,
            session,
            cols=cols,
            touch=True,
            transport=current_transport() or _stdio_transport,
        )
        payload["resumed"] = target
        # A lazy watch session never owns a run loop, so its payload's running
        # flag is always False — overlay the child-run registry so a reconnecting
        # watch window keeps its busy indicator while the child is still mid-run.
        if session.get("agent") is None and _child_run_active(target):
            payload["running"] = True
            payload["status"] = "streaming"
        return payload

    # Fast path: if the session is already live, reuse it under the lock.
    with _session_resume_lock:
        live = _find_live_session_by_key(target)
        if live is not None:
            return _ok(rid, _reuse_live_payload(*live))

    # Lazy/watch resume: register the live session WITHOUT building an agent.
    # Used by the desktop's subagent windows — the child runs inside the
    # parent's turn, so its window only needs the stored history plus a
    # transport for the child-mirror's live events. Skipping _make_agent here
    # is what keeps the window cheap while the backend is busy running the
    # delegation. A later prompt.submit upgrades it via _start_agent_build
    # (resume_session_id keeps the upgrade on the stored conversation).
    if is_truthy_value(params.get("lazy", False)):
        sid = uuid.uuid4().hex[:8]
        lease, limit_message = _claim_active_session_slot(target, live_session_id=sid)
        if limit_message is not None:
            return _err(rid, 4090, limit_message)
        try:
            db.reopen_session(target)
            # The child's OWN conversation only. Delegation children are
            # parent-linked rows, so include_ancestors would prepend the
            # parent's entire transcript — a watch window opened on a subagent
            # must show the subagent's branch, not the parent's prompt.
            history = db.get_messages_as_conversation(target)
        except Exception as e:
            if lease is not None:
                lease.release()
            return _err(rid, 5000, f"resume failed: {e}")
        messages = _history_to_messages(history)
        cwd = profile_resume_cwd or os.getenv("TERMINAL_CWD", os.getcwd())
        now = time.time()
        # A delegated child mid-run emits no native session events of its own —
        # report its liveness from the relay registry so the window paints a
        # busy indicator instead of a dead idle transcript.
        child_running = _child_run_active(target)
        with _session_resume_lock:
            live = _find_live_session_by_key(target)
            if live is not None:
                if lease is not None:
                    lease.release()
                return _ok(rid, _reuse_live_payload(*live))
            with _sessions_lock:
                _sessions[sid] = {
                    "agent": None,
                    "agent_error": None,
                    "agent_ready": threading.Event(),
                    "attached_images": [],
                    "close_on_disconnect": is_truthy_value(
                        params.get("close_on_disconnect", False)
                    ),
                    "active_session_lease": lease,
                    "cols": cols,
                    "created_at": now,
                    "display_history_prefix": [],
                    "edit_snapshots": {},
                    "explicit_cwd": False,
                    "history": history,
                    "history_lock": threading.Lock(),
                    "history_version": 0,
                    "image_counter": 0,
                    "cwd": cwd,
                    "inflight_turn": None,
                    "last_active": now,
                    "lazy": True,
                    "pending_title": None,
                    "profile_home": str(profile_home) if profile_home is not None else None,
                    "resume_session_id": target,
                    "running": False,
                    "session_key": target,
                    "show_reasoning": _load_show_reasoning(),
                    "slash_worker": None,
                    "tool_progress_mode": _load_tool_progress_mode(),
                    "tool_started_at": {},
                    "transport": current_transport() or _stdio_transport,
                }
                _register_session_cwd(_sessions[sid])
        return _ok(
            rid,
            {
                "session_id": sid,
                "resumed": target,
                "message_count": len(messages),
                "messages": messages,
                "info": {
                    "cwd": cwd,
                    "branch": _git_branch_for_cwd(cwd),
                    "model": _resolve_model(),
                    "tools": {},
                    "skills": {},
                    "lazy": True,
                    "desktop_contract": DESKTOP_BACKEND_CONTRACT,
                    "profile_name": _current_profile_name(),
                },
                "inflight": None,
                "running": child_running,
                "session_key": target,
                "started_at": now,
                "status": "streaming" if child_running else "idle",
            },
        )

    # Build the agent OUTSIDE the lock — _make_agent can block for seconds
    # (MCP discovery, prompt/skill build, AIAgent construction). Holding
    # _session_resume_lock across it would stall session.close on the main
    # dispatch thread (it's not a _LONG_HANDLER), blocking fast-path RPCs.
    sid = uuid.uuid4().hex[:8]
    lease, limit_message = _claim_active_session_slot(target, live_session_id=sid)
    if limit_message is not None:
        return _err(rid, 4090, limit_message)
    _enable_gateway_prompts()
    home_token = (
        set_hermes_home_override(str(profile_home)) if profile_home is not None else None
    )
    try:
        db.reopen_session(target)
        history = db.get_messages_as_conversation(target)
        display_history = db.get_messages_as_conversation(
            target, include_ancestors=True
        )
        display_history_prefix = display_history[
            : max(0, len(display_history) - len(history))
        ]
        messages = _history_to_messages(display_history)
        tokens = _set_session_context(target)
        try:
            # Pass the profile's db so the agent persists turns to the right
            # state.db; home override is active here so config/skills/model
            # resolve to the profile too. Runtime identity is restored from the
            # stored session row so switching chats does not inherit whatever
            # global model another chat last selected.
            stored_runtime_overrides = _stored_session_runtime_overrides(found)
            agent = _make_agent(
                sid,
                target,
                session_id=target,
                session_db=db,
                **stored_runtime_overrides,
            )
        finally:
            _clear_session_context(tokens)
    except Exception as e:
        if lease is not None:
            lease.release()
        return _err(rid, 5000, f"resume failed: {e}")
    finally:
        if home_token is not None:
            reset_hermes_home_override(home_token)

    # Double-checked locking: another concurrent resume may have created the
    # live session while we were building. Re-check under the lock; if it won,
    # discard our just-built agent and reuse theirs (no worker/poller wired yet).
    with _session_resume_lock:
        live = _find_live_session_by_key(target)
        if live is not None:
            try:
                if hasattr(agent, "close"):
                    agent.close()
            except Exception:
                pass
            if lease is not None:
                lease.release()
            other_sid, other_session = live
            payload = _live_session_payload(
                other_sid,
                other_session,
                cols=cols,
                touch=True,
                transport=current_transport() or _stdio_transport,
            )
            payload["resumed"] = target
            return _ok(rid, payload)
        try:
            init_home_token = (
                set_hermes_home_override(str(profile_home))
                if profile_home is not None
                else None
            )
            try:
                _init_session(
                    sid,
                    target,
                    agent,
                    history,
                    cols=cols,
                    cwd=profile_resume_cwd,
                    session_db=db,
                )
            finally:
                if init_home_token is not None:
                    reset_hermes_home_override(init_home_token)
            if sid in _sessions:
                if stored_runtime_overrides.get("model_override") is not None:
                    _sessions[sid]["model_override"] = stored_runtime_overrides[
                        "model_override"
                    ]
                _sessions[sid]["display_history_prefix"] = display_history_prefix
                # Remember the profile home so each turn re-binds HERMES_HOME (the
                # agent persists to its own db, but mid-turn home reads — memory,
                # skills — must resolve to the resumed profile too).
                if profile_home is not None:
                    _sessions[sid]["profile_home"] = str(profile_home)
                _sessions[sid]["active_session_lease"] = lease
        except Exception as e:
            if lease is not None:
                lease.release()
            return _err(rid, 5000, f"resume failed: {e}")
        session = _sessions.get(sid) or {}
    return _ok(
        rid,
        {
            "session_id": sid,
            "resumed": target,
            "message_count": len(messages),
            "messages": messages,
            "info": _session_info(agent, session),
            "inflight": None,
            "running": False,
            "session_key": target,
            "started_at": float(session.get("created_at") or time.time()),
            "status": "idle",
        },
    )


@method("session.cwd.set")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    if session.get("running"):
        return _err(rid, 4009, "session busy")
    raw = str(params.get("cwd", "") or "").strip()
    if not raw:
        return _err(rid, 4016, "cwd required")
    try:
        cwd = _set_session_cwd(session, raw)
    except ValueError as e:
        return _err(rid, 4017, str(e))
    agent = session.get("agent")
    info = _session_info(agent, session) if agent is not None else {
        "cwd": cwd,
        "branch": _git_branch_for_cwd(cwd),
        "lazy": True,
    }
    _emit("session.info", params.get("session_id", ""), info)
    return _ok(rid, info)


def _session_pending_kind(sid: str) -> str:
    for rid, (owner_sid, _ev) in list(_pending.items()):
        if owner_sid != sid:
            continue
        event, _payload = _pending_prompt_payloads.get(rid, ("input.request", {}))
        return str(event).removesuffix(".request")
    return ""


def _session_live_status(sid: str, session: dict) -> str:
    if _session_pending_kind(sid):
        return "waiting"
    ready = session.get("agent_ready")
    # Unset + build never started = a lazy watch session sitting idle, not a
    # session stuck mid-construction.
    if ready is not None and not ready.is_set() and session.get("agent_build_started"):
        return "starting"
    if session.get("running"):
        return "working"
    return "idle"


def _message_preview(history: list) -> str:
    for msg in reversed(history or []):
        text = _content_display_text(msg.get("content", msg.get("text", ""))).strip()
        if text:
            return " ".join(text.split())[:160]
    return ""


def _session_live_title(session: dict, key: str) -> str:
    title = str(session.get("pending_title") or "").strip()
    db = _get_db()
    if db is not None:
        try:
            title = str(db.get_session_title(key) or title or "").strip()
        except Exception:
            pass
    return title


def _session_live_item(sid: str, session: dict, current_sid: str = "") -> dict:
    key = str(session.get("session_key") or sid)
    agent = session.get("agent")
    history = list(session.get("history") or [])
    status = _session_live_status(sid, session)
    inflight = _inflight_snapshot(session)
    preview = _message_preview(history)
    if inflight:
        preview = inflight.get("assistant") or inflight.get("user") or preview
        preview = " ".join(str(preview).split())[:160]
    now = time.time()
    return {
        "current": sid == current_sid,
        "id": sid,
        "last_active": float(session.get("last_active") or session.get("created_at") or now),
        "message_count": len(history),
        "model": str(getattr(agent, "model", "") or _resolve_model()),
        "preview": preview,
        "session_key": key,
        "started_at": float(session.get("created_at") or now),
        "status": status,
        "title": _session_live_title(session, key),
    }


def _find_live_session_by_key(session_key: str) -> tuple[str, dict] | None:
    for sid, session in list(_sessions.items()):
        if session.get("_finalized"):
            continue
        if str(session.get("session_key") or "") == session_key:
            return sid, session
    return None


def _fallback_session_info(session: dict) -> dict:
    agent = session.get("agent")
    if agent is not None:
        return _session_info(agent)
    return {
        "cwd": os.getenv("TERMINAL_CWD", os.getcwd()),
        "lazy": True,
        "model": _resolve_model(),
        "skills": {},
        "tools": {},
    }


def _live_session_payload(
    sid: str,
    session: dict,
    *,
    cols: int | None = None,
    touch: bool = False,
    transport: Transport | None = None,
) -> dict:
    with session["history_lock"]:
        if cols is not None:
            session["cols"] = cols
        if transport is not None:
            session["transport"] = transport
        if touch:
            session["last_active"] = time.time()
        history = list(session.get("display_history_prefix") or []) + list(
            session.get("history") or []
        )
        inflight = _inflight_snapshot(session)
        running = bool(session.get("running"))
    payload = {
        "info": _fallback_session_info(session),
        "message_count": len(history),
        "messages": _history_to_messages(history),
        "running": running,
        "session_id": sid,
        "session_key": session.get("session_key") or sid,
        "started_at": float(session.get("created_at") or time.time()),
        "status": _session_live_status(sid, session),
    }
    if inflight:
        payload["inflight"] = inflight
    return payload


@method("session.active_list")
def _(rid, params: dict) -> dict:
    """Return live TUI sessions in this gateway process.

    Unlike ``session.list`` this is not a historical DB browser: it reports only
    sessions with in-memory agents/workers that the current TUI can switch to
    without closing siblings.
    """
    current = str(params.get("current_session_id") or "")
    try:
        with _sessions_lock:
            snapshot = list(_sessions.items())
    except Exception as e:
        return _err(rid, 5036, f"could not enumerate active sessions: {e}")

    # Liveness filter (#38950): a session whose teardown has begun (``_finalized``)
    # is dead — its agent/worker are being released and it is no longer
    # attachable — but it can briefly remain in ``_sessions`` until the reaper
    # pops it (the WS grace-reap and idle reaper both set ``_finalized`` inside
    # ``_teardown_session`` before the pop). Counting these inflated the footer's
    # "N sessions" count, which only ever went up until a gateway restart. Drop
    # them here so the count reflects genuinely attachable sessions. We do NOT
    # filter on ``transport is _detached_ws_transport`` (the WS-detached drop
    # sentinel): a detached session is still attachable via a quick reconnect /
    # session.resume until the grace-reap finalizes it, and a standalone
    # ``hermes --tui`` session legitimately rides the real stdio transport and
    # must stay visible.
    # Keep the natural creation/insertion order from ``_sessions``.  The
    # frontend marks the focused session with ``current``; it should not jump to
    # the top just because the user switched to it.
    rows = [
        _session_live_item(sid, session, current)
        for sid, session in snapshot
        if not session.get("_finalized")
    ]
    return _ok(rid, {"sessions": rows})


@method("session.activate")
def _(rid, params: dict) -> dict:
    """Attach the frontend to an already-live TUI session.

    This intentionally does not close the previously focused session; it merely
    returns enough state for Ink to redraw around another live session id.
    """
    sid = str(params.get("session_id") or "")
    session, err = _sess_nowait({"session_id": sid}, rid)
    if err:
        return err
    assert session is not None

    return _ok(
        rid,
        _live_session_payload(
            sid,
            session,
            touch=True,
            transport=current_transport() or _stdio_transport,
        ),
    )


@method("session.delete")
def _(rid, params: dict) -> dict:
    """Delete a stored session and its on-disk transcript files.

    Used by the TUI resume picker (``d`` key) so users can prune old
    sessions without dropping to the CLI.  Refuses to delete a session
    that is currently active in this gateway process — those rows are
    still being written to and removing them out from under the live
    agent corrupts message ordering and trips FK constraints when the
    next message append flushes.
    """
    target = params.get("session_id", "")
    if not target:
        return _err(rid, 4006, "session_id required")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5036)
    # Block deletion of any session currently bound to a live TUI session
    # in this process.  The picker hides the active session anyway, but a
    # racing caller could still target it.  Snapshot via ``list(...)``
    # because ``_sessions`` is mutated by concurrent RPCs on the thread
    # pool — iterating the dict directly can raise ``RuntimeError:
    # dictionary changed size during iteration``.  If even the snapshot
    # raises, fail closed (refuse the delete) rather than fail open.
    try:
        with _sessions_lock:
            snapshot = list(_sessions.values())
    except Exception as e:
        return _err(rid, 5036, f"could not enumerate active sessions: {e}")
    active = {s.get("session_key") for s in snapshot if s.get("session_key")}
    if target in active:
        return _err(rid, 4023, "cannot delete an active session")
    sessions_dir = get_hermes_home() / "sessions"
    try:
        deleted = db.delete_session(target, sessions_dir=sessions_dir)
    except Exception as e:
        return _err(rid, 5036, f"delete failed: {e}")
    if not deleted:
        # Mirror the methods/session.py orphan-cleanup path: if the
        # sessions row is gone but session_index still carries an entry
        # (e.g. a session created via session_index projection whose
        # creation flow failed before any sessions-table insert), sweep
        # the index row so the sidebar stops resurfacing the ghost.
        index_removed = 0
        if hasattr(db, "delete_session_index"):
            try:
                index_removed = int(db.delete_session_index(target) or 0)
            except Exception:
                logger.debug(
                    "session.delete: session_index cleanup failed", exc_info=True
                )
        if index_removed > 0:
            return _ok(rid, {"deleted": target, "via": "session_index_cleanup"})
        return _err(rid, 4007, "session not found")
    if hasattr(db, "delete_session_index"):
        try:
            db.delete_session_index(target)
        except Exception:
            logger.debug(
                "session.delete: session_index post-sweep failed", exc_info=True
            )
    return _ok(rid, {"deleted": target})


@method("session.title")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5007)
    key = session["session_key"]
    if "title" not in params:
        fallback = session.get("pending_title") or ""
        try:
            resolved_title = db.get_session_title(key) or ""
            if fallback:
                if db.set_session_title(key, fallback):
                    session["pending_title"] = None
                    resolved_title = fallback
                else:
                    existing_row = db.get_session(key)
                    existing_title = ((existing_row or {}).get("title") or "").strip()
                    if existing_title == fallback:
                        session["pending_title"] = None
                        resolved_title = fallback
                    elif not resolved_title:
                        resolved_title = fallback
            elif resolved_title:
                session["pending_title"] = None
        except Exception:
            resolved_title = fallback
        return _ok(
            rid,
            {
                "title": resolved_title,
                "session_key": key,
            },
        )
    title = (params.get("title", "") or "").strip()
    if not title:
        return _err(rid, 4021, "title required")
    try:
        if db.set_session_title(key, title):
            session["pending_title"] = None
            return _ok(rid, {"pending": False, "title": title})
        # rowcount == 0 can mean "same value" as well as "missing row".
        existing_row = db.get_session(key)
        if existing_row:
            session["pending_title"] = None
            return _ok(
                rid,
                {
                    "pending": False,
                    "title": (existing_row.get("title") or title),
                },
            )
        # No row yet (the DB write is deferred to the first prompt so empty
        # drafts don't litter the sidebar). An explicit /title is clear user
        # intent, not an abandoned draft — so persist the row NOW and set the
        # title, mirroring the messaging gateway's _handle_title_command. The
        # old behavior only queued pending_title and relied on the post-turn
        # apply block; if that turn never landed under this session_key the
        # title was silently lost and the sidebar fell back to the message
        # preview. Creating the row up front removes that race entirely. The
        # min-messages sidebar filter keeps a titled 0-message row hidden, so
        # a /title'd-but-never-used draft still doesn't clutter the list.
        _ensure_session_db_row(session)
        with _session_db(session) as scoped_db:
            if scoped_db is not None and scoped_db.set_session_title(key, title):
                session["pending_title"] = None
                return _ok(rid, {"pending": False, "title": title})
        # Row creation didn't take (DB unavailable, or a concurrent writer) —
        # fall back to queuing so the post-turn apply block can still recover.
        session["pending_title"] = title
        return _ok(rid, {"pending": True, "title": title})
    except ValueError as e:
        return _err(rid, 4022, str(e))
    except Exception as e:
        return _err(rid, 5007, str(e))


@method("handoff.request")
def _(rid, params: dict) -> dict:
    """Queue a handoff of this session to a messaging platform.

    Desktop parity with the CLI ``/handoff`` command: we only write
    ``handoff_state='pending'`` onto the persisted session row. The actual
    transfer is performed by the separate ``hermes gateway`` process, whose
    ``_handoff_watcher`` claims the row, re-binds the session to the platform's
    home channel, and forges a synthetic turn. The desktop then polls
    ``handoff.state`` for the terminal result.
    """
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    if session.get("running"):
        return _err(
            rid,
            4009,
            "session busy — wait for the current turn to finish, then retry the handoff",
        )

    platform_name = (params.get("platform", "") or "").strip().lower()
    if not platform_name:
        return _err(rid, 4023, "platform required")

    # Validate against the live gateway config — an unconfigured platform or a
    # missing home channel would leave the handoff pending forever, so reject
    # up front with a clear, actionable message (mirrors cli.py).
    try:
        from gateway.config import Platform, load_gateway_config
    except Exception as e:  # pragma: no cover — gateway pkg always ships
        return _err(rid, 5021, f"could not load gateway config: {e}")
    try:
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        return _err(rid, 4024, f"unknown platform '{platform_name}'")
    try:
        gw_config = load_gateway_config()
    except Exception as e:
        return _err(rid, 5021, f"could not load gateway config: {e}")
    pcfg = gw_config.platforms.get(platform)
    if not pcfg or not pcfg.enabled:
        return _err(
            rid,
            4025,
            f"platform '{platform_name}' is not configured/enabled in the gateway",
        )
    home = gw_config.get_home_channel(platform)
    if not home or not home.chat_id:
        return _err(
            rid,
            4026,
            f"no home channel configured for {platform_name} — set one with "
            "/sethome on the destination chat first",
        )

    # The watcher transfers a persisted DB row, so make sure one exists even
    # for a brand-new empty chat (mirrors the CLI's set_session_title stub).
    _ensure_session_db_row(session)

    with _session_db(session) as db:
        if db is None:
            return _db_unavailable_error(rid, code=5007)
        key = session["session_key"]
        try:
            if not db.get_session(key):
                db.set_session_title(key, f"handoff-{key[:8]}")
            ok = db.request_handoff(key, platform_name)
        except Exception as e:
            return _err(rid, 5007, str(e))

    if not ok:
        return _err(
            rid,
            4027,
            "session is already in flight for handoff — wait for it to settle, then retry",
        )
    return _ok(
        rid,
        {
            "queued": True,
            "session_key": key,
            "platform": platform_name,
            "home_name": home.name,
        },
    )


@method("handoff.state")
def _(rid, params: dict) -> dict:
    """Poll the handoff state for a session.

    Returns ``{state, platform, error}`` where ``state`` is one of
    ``pending|running|completed|failed`` (or empty when no handoff record
    exists). Desktop polls this after ``handoff.request``.
    """
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    with _session_db(session) as db:
        if db is None:
            return _db_unavailable_error(rid, code=5007)
        record = db.get_handoff_state(session["session_key"])

    record = record or {}
    return _ok(
        rid,
        {
            "state": record.get("state") or "",
            "platform": record.get("platform") or "",
            "error": record.get("error") or "",
        },
    )


@method("handoff.fail")
def _(rid, params: dict) -> dict:
    """Mark an in-flight handoff as failed so the user can retry.

    Desktop calls this when its bounded poll times out. Only pending/running
    rows are changed so a late success from the gateway watcher is not clobbered.
    """
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    reason = str(params.get("error") or "handoff failed").strip()[:500]
    with _session_db(session) as db:
        if db is None:
            return _db_unavailable_error(rid, code=5007)
        key = session["session_key"]
        record = db.get_handoff_state(key) or {}
        state = record.get("state") or ""
        if state in {"pending", "running"}:
            db.fail_handoff(key, reason)
            return _ok(rid, {"failed": True, "state": "failed"})

    return _ok(rid, {"failed": False, "state": state})


@method("session.usage")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    agent = session.get("agent")
    usage: dict = (
        _get_usage(agent)
        if agent is not None
        else {"calls": 0, "input": 0, "output": 0, "total": 0}
    )
    # Nous credits block — agent-independent (a portal fetch), so it shows even
    # with zero API calls or on a resumed session. The TUI /usage panel renders
    # these lines regardless of `calls`. Fail-open: [] when not logged into Nous
    # or on any portal hiccup.
    try:
        from agent.account_usage import nous_credits_lines

        credits = nous_credits_lines()
        if credits:
            usage["credits_lines"] = credits
    except Exception:
        pass
    return _ok(rid, usage)


@method("credits.view")
def _(rid, params: dict) -> dict:
    """Structured Nous credit view for the TUI /credits command.

    Account-independent (a portal fetch gated on "a Nous account is logged in"),
    so it works with no live agent / on a resumed session — same as the /usage
    credits block. Returns the surface-agnostic CreditsView fields so the TUI can
    render a clickable top-up <Link>. Fail-open: a portal hiccup or logged-out
    account yields {logged_in: false}, never an error the user has to parse.
    """
    try:
        from agent.account_usage import build_credits_view

        view = build_credits_view()
        return _ok(
            rid,
            {
                "logged_in": bool(view.logged_in),
                "balance_lines": [
                    line for line in view.balance_lines if not line.lstrip().startswith("📈")
                ],
                "identity_line": view.identity_line,
                "topup_url": view.topup_url,
                "depleted": bool(view.depleted),
            },
        )
    except Exception:
        # Fail-open: TUI treats this as "not logged in" and shows the prompt.
        return _ok(rid, {"logged_in": False, "balance_lines": [], "identity_line": None, "topup_url": None, "depleted": False})


# ===========================================================================
# Phase 2b terminal billing RPC methods
# ===========================================================================
#
# These return STRUCTURED success envelopes (result.ok / result.error) rather
# than JSON-RPC-level errors, so the TUI's rpc() promise always resolves and the
# Ink side can branch on the typed billing error code (insufficient_scope,
# rate_limited, no_payment_method, …) to render the right affordance instead of
# landing in a generic catch. The data-building lives in the shared core
# (agent/billing_view.py + hermes_cli/nous_billing.py) — same as /credits.


def _serialize_billing_error(exc) -> dict:
    """Map a BillingError into the result.error envelope the TUI branches on."""
    from hermes_cli.nous_billing import (
        BillingRateLimited,
        BillingScopeRequired,
    )

    kind = "error"
    if isinstance(exc, BillingScopeRequired):
        kind = "insufficient_scope"
    elif isinstance(exc, BillingRateLimited):
        kind = "rate_limited"
    elif getattr(exc, "error", None):
        kind = str(exc.error)
    return {
        "ok": False,
        "error": kind,
        "message": str(exc),
        "portal_url": getattr(exc, "portal_url", None),
        "retry_after": getattr(exc, "retry_after", None),
        "payload": getattr(exc, "payload", {}) or {},
    }


def _serialize_billing_state(state) -> dict:
    """Serialize a BillingState for the wire (Decimals → strings, money-safe)."""
    from agent.billing_view import format_money

    def _s(value):
        return None if value is None else str(value)

    card = None
    if state.card is not None:
        card = {"brand": state.card.brand, "last4": state.card.last4, "masked": state.card.masked}
    monthly_cap = None
    if state.monthly_cap is not None:
        mc = state.monthly_cap
        monthly_cap = {
            "limit_usd": _s(mc.limit_usd),
            "limit_display": format_money(mc.limit_usd),
            "spent_this_month_usd": _s(mc.spent_this_month_usd),
            "spent_display": format_money(mc.spent_this_month_usd),
            "is_default_ceiling": mc.is_default_ceiling,
        }
    auto_reload = None
    if state.auto_reload is not None:
        ar = state.auto_reload
        auto_reload = {
            "enabled": ar.enabled,
            "threshold_usd": _s(ar.threshold_usd),
            "threshold_display": format_money(ar.threshold_usd),
            "reload_to_usd": _s(ar.reload_to_usd),
            "reload_to_display": format_money(ar.reload_to_usd),
        }
    return {
        "ok": True,
        "logged_in": state.logged_in,
        "org_name": state.org_name,
        "org_slug": state.org_slug,
        "role": state.role,
        "is_admin": state.is_admin,
        "can_charge": state.can_charge,
        "balance_usd": _s(state.balance_usd),
        "balance_display": format_money(state.balance_usd),
        "cli_billing_enabled": state.cli_billing_enabled,
        "charge_presets": [_s(p) for p in state.charge_presets],
        "charge_presets_display": [format_money(p) for p in state.charge_presets],
        "min_usd": _s(state.min_usd),
        "max_usd": _s(state.max_usd),
        "card": card,
        "monthly_cap": monthly_cap,
        "auto_reload": auto_reload,
        "portal_url": state.portal_url,
        "error": state.error,
    }


@method("billing.state")
def _(rid, params: dict) -> dict:
    """GET /api/billing/state → serialized BillingState (Screen 1 + 5).

    Fail-open like credits.view: a logged-out / unreachable portal yields
    {ok:true, logged_in:false}. No scope required for this endpoint.
    """
    try:
        from agent.billing_view import build_billing_state

        state = build_billing_state()
        return _ok(rid, _serialize_billing_state(state))
    except Exception:
        return _ok(rid, {"ok": True, "logged_in": False, "error": "could not load billing state"})


@method("billing.charge")
def _(rid, params: dict) -> dict:
    """POST /api/billing/charge → {ok, chargeId} or a typed error envelope.

    params: {amount_usd: str|number, idempotency_key?: str}. If no key is
    supplied, the server-side core mints a fresh one and returns it so the TUI can
    reuse it on retry of the SAME purchase.
    """
    from hermes_cli.nous_billing import BillingError, post_charge
    from agent.billing_view import new_idempotency_key

    amount = params.get("amount_usd")
    if amount is None:
        return _ok(rid, {"ok": False, "error": "invalid_request", "message": "amount_usd is required"})
    key = params.get("idempotency_key") or new_idempotency_key()
    try:
        result = post_charge(amount_usd=amount, idempotency_key=key)
        return _ok(rid, {"ok": True, "charge_id": result.get("chargeId"), "idempotency_key": key})
    except BillingError as exc:
        env = _serialize_billing_error(exc)
        env["idempotency_key"] = key  # so the TUI can reuse on retry
        return _ok(rid, env)
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc), "idempotency_key": key})


@method("billing.charge_status")
def _(rid, params: dict) -> dict:
    """GET /api/billing/charge/{id} → {ok, status, ...} or typed error.

    The poll. Caller drives the 2s/5-min cadence; this is a single status read.
    """
    from hermes_cli.nous_billing import BillingError, get_charge_status

    charge_id = params.get("charge_id")
    if not charge_id:
        return _ok(rid, {"ok": False, "error": "invalid_charge_id", "message": "charge_id is required"})
    try:
        result = get_charge_status(charge_id)
        return _ok(
            rid,
            {
                "ok": True,
                "status": result.get("status"),
                "amount_usd": result.get("amountUsd"),
                "settled_at": result.get("settledAt"),
                "reason": result.get("reason"),
            },
        )
    except BillingError as exc:
        return _ok(rid, _serialize_billing_error(exc))
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc)})


@method("billing.auto_reload")
def _(rid, params: dict) -> dict:
    """PATCH /api/billing/auto-top-up → {ok:true} or typed error (Screen 2).

    params: {enabled: bool, threshold: number, top_up_amount: number}.
    """
    from hermes_cli.nous_billing import BillingError, patch_auto_top_up

    try:
        enabled = bool(params.get("enabled"))
        threshold = params.get("threshold")
        top_up_amount = params.get("top_up_amount")
        if threshold is None or top_up_amount is None:
            return _ok(rid, {"ok": False, "error": "invalid_request", "message": "threshold and top_up_amount are required"})
        patch_auto_top_up(enabled=enabled, threshold=threshold, top_up_amount=top_up_amount)
        return _ok(rid, {"ok": True})
    except BillingError as exc:
        return _ok(rid, _serialize_billing_error(exc))
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc)})


@method("billing.step_up")
def _(rid, params: dict) -> dict:
    """Run the lazy billing:manage step-up device flow → {ok, granted}.

    Triggered by the TUI after a billing call returns error=insufficient_scope.
    Returns granted:false when the server silently downscopes (non-admin / unticked).

    Runs on the thread pool (in _LONG_HANDLERS): the device flow blocks for the
    whole device-code lifetime (minutes), so it must not stall the main stdin loop.
    The verification URL/code reach the TUI via an out-of-band ``billing.step_up.
    verification`` event (a plain print would be dropped by the JSON-RPC stdout
    pipe), and the browser is opened TUI-side via openExternalUrl — never with the
    gateway's headless webbrowser.open (hence open_browser=False).
    """
    sid = params.get("session_id") or ""
    try:
        from hermes_cli.auth import step_up_nous_billing_scope

        def _on_verification(url: str, code: str) -> None:
            _emit(
                "billing.step_up.verification",
                sid,
                {"verification_url": url, "user_code": code},
            )

        granted = step_up_nous_billing_scope(
            open_browser=False, on_verification=_on_verification
        )
        return _ok(rid, {"ok": True, "granted": bool(granted)})
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc), "granted": False})


@method("session.status")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err

    from hermes_constants import display_hermes_home

    key = session.get("session_key") or params.get("session_id") or ""
    agent = session.get("agent")
    meta = {}
    db = _get_db()
    if db and key:
        try:
            meta = db.get_session(key) or {}
        except Exception:
            meta = {}

    def _dt(value, fallback: datetime | None = None) -> datetime:
        if value:
            try:
                return datetime.fromtimestamp(float(value))
            except Exception:
                pass
        return fallback or datetime.now()

    created = _dt(meta.get("started_at"))
    updated = created
    for field in ("updated_at", "last_updated_at", "last_activity_at"):
        if meta.get(field):
            updated = _dt(meta.get(field), created)
            break

    usage = _get_usage(agent) if agent is not None else {}
    provider = getattr(agent, "provider", None) or "unknown"
    model = getattr(agent, "model", None) or "(unknown)"
    lines = [
        "Hermes TUI Status",
        "",
        f"Session ID: {key}",
        f"Path: {display_hermes_home()}",
    ]
    title = (meta.get("title") or "").strip()
    if title:
        lines.append(f"Title: {title}")
    lines.extend(
        [
            f"Model: {model} ({provider})",
            f"Created: {created.strftime('%Y-%m-%d %H:%M')}",
            f"Last Activity: {updated.strftime('%Y-%m-%d %H:%M')}",
            f"Tokens: {int(usage.get('total') or 0):,}",
            f"Agent Running: {'Yes' if session.get('running') else 'No'}",
        ]
    )
    return _ok(rid, {"output": "\n".join(lines)})


@method("session.history")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    history = list(session.get("history", []))
    db = _get_db()
    if db is not None and session.get("session_key"):
        try:
            history = db.get_messages_as_conversation(
                session["session_key"], include_ancestors=True
            )
        except Exception:
            pass
    return _ok(
        rid,
        {
            "count": len(history),
            "messages": _history_to_messages(history),
        },
    )


@method("session.undo")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    # Reject during an in-flight turn.  If we mutated history while
    # the agent thread is running, prompt.submit's post-run history
    # write would either clobber the undo (version matches) or
    # silently drop the agent's output (version mismatch, see below).
    # Neither is what the user wants — make them /interrupt first.
    if session.get("running"):
        return _err(
            rid, 4009, "session busy — /interrupt the current turn before /undo"
        )
    removed = 0
    with session["history_lock"]:
        history = session.get("history", [])
        while history and history[-1].get("role") in {"assistant", "tool"}:
            history.pop()
            removed += 1
        if history and history[-1].get("role") == "user":
            history.pop()
            removed += 1
        if removed:
            session["history_version"] = int(session.get("history_version", 0)) + 1
    return _ok(rid, {"removed": removed})


@method("session.compress")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    if session.get("running"):
        return _err(
            rid, 4009, "session busy — /interrupt the current turn before /compress"
        )
    sid = params.get("session_id", "")
    focus_topic = str(params.get("focus_topic", "") or "").strip()
    try:
        from agent.manual_compression_feedback import summarize_manual_compression
        from agent.model_metadata import estimate_request_tokens_rough

        with session["history_lock"]:
            before_messages = list(session.get("history", []))
            history_version = int(session.get("history_version", 0))
        before_count = len(before_messages)
        _agent = session["agent"]
        _sys_prompt = getattr(_agent, "_cached_system_prompt", "") or ""
        _tools = getattr(_agent, "tools", None) or None
        before_tokens = (
            estimate_request_tokens_rough(
                before_messages, system_prompt=_sys_prompt, tools=_tools
            )
            if before_count
            else 0
        )

        if before_count >= 4:
            focus_suffix = f', focus: "{focus_topic}"' if focus_topic else ""
            _status_update(
                sid,
                "compressing",
                f"⠋ compressing {before_count} messages "
                f"(~{before_tokens:,} tok){focus_suffix}…",
            )

        try:
            removed, usage = _compress_session_history(
                session,
                focus_topic,
                approx_tokens=before_tokens,
                before_messages=before_messages,
                history_version=history_version,
            )
            with session["history_lock"]:
                messages = list(session.get("history", []))
            after_count = len(messages)
            # Re-read system prompt + tools after compression — _compress_context
            # may have rebuilt the system prompt (_cached_system_prompt=None).
            _sys_prompt_after = (
                getattr(_agent, "_cached_system_prompt", "") or _sys_prompt
            )
            _tools_after = getattr(_agent, "tools", None) or _tools
            after_tokens = (
                estimate_request_tokens_rough(
                    messages,
                    system_prompt=_sys_prompt_after,
                    tools=_tools_after,
                )
                if after_count
                else 0
            )
            agent = session["agent"]
            _sync_session_key_after_compress(sid, session)
            summary = summarize_manual_compression(
                before_messages, messages, before_tokens, after_tokens
            )
            info = _session_info(agent, session)
            _emit("session.info", sid, info)
            return _ok(
                rid,
                {
                    "status": "compressed",
                    "removed": removed,
                    "before_messages": before_count,
                    "after_messages": after_count,
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "summary": summary,
                    "usage": usage,
                    "info": info,
                    "messages": messages,
                },
            )
        finally:
            # Always clear the pinned compressing status so the bar
            # reverts to neutral whether compaction succeeded, was a
            # no-op, or raised.
            _status_update(sid, "ready")
    except Exception as e:
        return _err(rid, 5005, str(e))


@method("session.save")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err

    agent = session["agent"]
    # Mirror the classic CLI /save: snapshot under the Hermes profile home
    # (~/.hermes/sessions/saved/) rather than the project/workspace CWD, and
    # include the system prompt so the export matches the dashboard save.
    saved_dir = get_hermes_home() / "sessions" / "saved"
    try:
        saved_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return _err(rid, 5011, f"failed to create save directory {saved_dir}: {e}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = saved_dir / f"hermes_conversation_{timestamp}.json"

    with session["history_lock"]:
        messages = list(session.get("history", []))

    session_id = getattr(agent, "session_id", None) or session.get("session_key") or ""
    # Prefer the agent's session_start datetime (matches the classic CLI export);
    # fall back to the gateway session's created_at timestamp.
    agent_start = getattr(agent, "session_start", None)
    if isinstance(agent_start, datetime):
        session_start = agent_start.isoformat()
    else:
        created_at = session.get("created_at")
        session_start = (
            datetime.fromtimestamp(created_at).isoformat()
            if isinstance(created_at, (int, float))
            else ""
        )

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model": getattr(agent, "model", ""),
                    "session_id": session_id,
                    "session_start": session_start,
                    "system_prompt": getattr(agent, "_cached_system_prompt", "") or "",
                    "messages": messages,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        return _ok(rid, {"file": str(path)})
    except Exception as e:
        return _err(rid, 5011, str(e))


@method("session.close")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "")
    # Serialize against the WS-orphan reaper (which also pops under
    # _session_resume_lock) so a disconnect-reap and an explicit close can't
    # both tear the same session down. _close_session_by_id is the single
    # idempotent teardown path (pop + _teardown_session) and returns False
    # when the session is already gone.
    with _session_resume_lock:
        return _ok(rid, {"closed": _close_session_by_id(sid, end_reason="tui_close")})


@method("session.branch")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    old_key = session["session_key"]
    with session["history_lock"]:
        history = [dict(msg) for msg in session.get("history", [])]
    if not history:
        return _err(rid, 4008, "nothing to branch — send a message first")
    new_key = _new_session_key()
    new_sid = uuid.uuid4().hex[:8]
    lease, limit_message = _claim_active_session_slot(new_key, live_session_id=new_sid)
    if limit_message is not None:
        return _err(rid, 4090, limit_message)
    branch_name = params.get("name", "")
    try:
        if branch_name:
            title = branch_name
        else:
            current = db.get_session_title(old_key) or "branch"
            title = (
                db.get_next_title_in_lineage(current)
                if hasattr(db, "get_next_title_in_lineage")
                else f"{current} (branch)"
            )
        db.create_session(
            new_key,
            source="tui",
            model=_resolve_model(),
            # Stable _branched_from marker so list_sessions_rich() keeps the
            # branch visible in /resume and /sessions. The TUI branch leaves
            # the parent live (no end_reason='branched'), so the legacy
            # end_reason heuristic never matches it — the marker is the only
            # thing that surfaces TUI branches. See issue #20856.
            model_config={"_branched_from": old_key},
            parent_session_id=old_key,
            cwd=_session_cwd(session),
        )
        for msg in history:
            db.append_message(
                session_id=new_key,
                role=msg.get("role", "user"),
                content=msg.get("content"),
            )
        db.set_session_title(new_key, title)
    except Exception as e:
        if lease is not None:
            lease.release()
        return _err(rid, 5008, f"branch failed: {e}")
    try:
        tokens = _set_session_context(new_key)
        try:
            agent = _make_agent(new_sid, new_key, session_id=new_key)
        finally:
            _clear_session_context(tokens)
        _init_session(
            new_sid, new_key, agent, list(history), cols=session.get("cols", 80)
        )
        if new_sid in _sessions:
            _sessions[new_sid]["active_session_lease"] = lease
    except Exception as e:
        if lease is not None:
            lease.release()
        return _err(rid, 5000, f"agent init failed on branch: {e}")
    return _ok(rid, {"session_id": new_sid, "title": title, "parent": old_key})


@method("session.interrupt")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    if hasattr(session["agent"], "interrupt"):
        session["agent"].interrupt()
    # Scope the pending-prompt release to THIS session.  A global
    # _clear_pending() would collaterally cancel clarify/sudo/secret
    # prompts on unrelated sessions sharing the same tui_gateway
    # process, silently resolving them to empty strings.
    _clear_pending(params.get("session_id", ""))
    try:
        from tools.approval import resolve_gateway_approval

        resolve_gateway_approval(session["session_key"], "deny", resolve_all=True)
    except Exception:
        pass
    return _ok(rid, {"status": "interrupted"})


# ── Delegation: subagent tree observability + controls ───────────────
# Powers the TUI's /agents overlay (see ui-tui/src/components/agentsOverlay).
# The registry lives in tools/delegate_tool — these handlers are thin
# translators between JSON-RPC and the Python API.


# ── Spawn-tree snapshots: TUI-written, disk-persisted ────────────────
# The TUI is the source of truth for subagent state (it assembles payloads
# from the event stream).  On turn-complete it posts the final tree here;
# /replay and /replay-diff fetch past snapshots by session_id + filename.
#
# Layout:  $HERMES_HOME/spawn-trees/<session_id>/<timestamp>.json
# Each file contains { session_id, started_at, finished_at, subagents: [...] }.


def _spawn_trees_root():
    from pathlib import Path as _P
    from hermes_constants import get_hermes_home

    root = get_hermes_home() / "spawn-trees"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _spawn_tree_session_dir(session_id: str):
    safe = (
        "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or "unknown"
    )
    d = _spawn_trees_root() / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


# Per-session append-only index of lightweight snapshot metadata.  Read by
# `spawn_tree.list` so scanning doesn't require reading every full snapshot
# file (Copilot review on #14045).  One JSON object per line.
_SPAWN_TREE_INDEX = "_index.jsonl"


def _append_spawn_tree_index(session_dir, entry: dict) -> None:
    try:
        with (session_dir / _SPAWN_TREE_INDEX).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        # Index is a cache — losing a line just means list() falls back
        # to a directory scan for that entry.  Never block the save.
        logger.debug("spawn_tree index append failed: %s", exc)


def _read_spawn_tree_index(session_dir) -> list[dict]:
    index_path = session_dir / _SPAWN_TREE_INDEX
    if not index_path.exists():
        return []
    out: list[dict] = []
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


# ── Methods: prompt ──────────────────────────────────────────────────


def _start_notification_poller(sid: str, session: dict) -> threading.Event:
    return _start_notification_poller_service(
        sid,
        session,
        emit=_emit,
        run_prompt_submit=_run_prompt_submit,
        resolve_event_session=_resolve_notification_event_session,
    )


def _resolve_notification_event_session(evt: dict) -> tuple[str, dict] | None:
    with _sessions_lock:
        snapshot = list(_sessions.items())
    for candidate_sid, candidate in snapshot:
        if _session_owns_notification_event(candidate, evt):
            return candidate_sid, candidate
    return None


def _notification_event_dedup_key(evt: dict) -> tuple:
    """Return the UI-emission identity for a process notification event.

    Completion events are terminal notifications for a background process, so
    they remain one-shot per process session. Watch-match events are not
    terminal: a single background process can legitimately match the same or
    different patterns many times, so include event-specific content to avoid
    suppressing later distinct matches from the same process.
    """
    evt_type = evt.get("type", "completion")
    evt_sid = evt.get("session_id", "")
    if evt_type == "watch_match":
        return (
            evt_sid,
            evt_type,
            evt.get("command", ""),
            evt.get("pattern", ""),
            evt.get("output", ""),
            evt.get("suppressed", 0),
            evt.get("message_id", ""),
        )
    if evt_type.startswith("watch_overflow_") or evt_type == "watch_disabled":
        return (
            evt_sid,
            evt_type,
            evt.get("command", ""),
            evt.get("message", ""),
            evt.get("suppressed", 0),
        )
    if evt_type == "async_delegation":
        # Async-delegation completions have no process session_id; without
        # this the fallthrough keys every one as ("", "async_delegation")
        # and the second completion's status update is suppressed forever.
        return (evt.get("delegation_id", ""), evt_type)
    return (evt_sid, evt_type)


def _notification_poller_loop(
    stop_event: threading.Event,
    sid: str,
    session: dict,
) -> None:
    return _notification_poller_loop_service(
        stop_event,
        sid,
        session,
        emit=_emit,
        run_prompt_submit=_run_prompt_submit,
        resolve_event_session=_resolve_notification_event_session,
    )


# ── Methods: respond ─────────────────────────────────────────────────


# ── Methods: config ──────────────────────────────────────────────────


@method("config.set")
def _(rid, params: dict) -> dict:
    key, value = params.get("key", ""), params.get("value", "")
    session = _sessions.get(params.get("session_id", ""))

    if key == "model":
        try:
            if not value:
                return _err(rid, 4002, "model value required")
            if session:
                # Reject during an in-flight turn.  agent.switch_model()
                # mutates self.model / self.provider / self.base_url /
                # self.client in place; the worker thread running
                # agent.run_conversation is reading those on every
                # iteration.  A mid-turn swap can send an HTTP request
                # with the new base_url but old model (or vice versa),
                # producing 400/404s the user never asked for.  Parity
                # with the gateway's running-agent /model guard.
                if session.get("running"):
                    return _err(
                        rid,
                        4009,
                        "session busy — /interrupt the current turn before switching models",
                    )
                from hermes_cli.model_switch import parse_model_flags

                parsed_flags = parse_model_flags(value)
                _model_input, explicit_provider, _persist_global, _force_refresh, _is_session = parsed_flags
                if session.get("agent") is None and not explicit_provider.strip():
                    session_id = params.get("session_id", "")
                    _start_agent_build(session_id, session)
                    init_err = _wait_agent(session, rid)
                    if init_err:
                        return init_err
                    if session.get("agent") is None:
                        return _err(rid, 5032, "agent initialization failed")
                result = _apply_model_switch(
                    params.get("session_id", ""),
                    session,
                    value,
                    confirm_expensive_model=bool(
                        params.get("confirm_expensive_model", False)
                    ),
                )
            else:
                result = _apply_model_switch(
                    "",
                    {"agent": None},
                    value,
                    confirm_expensive_model=bool(
                        params.get("confirm_expensive_model", False)
                    ),
                )
            return _ok(
                rid,
                {
                    "key": key,
                    "value": result["value"],
                    "warning": result["warning"],
                    "confirm_required": result.get("confirm_required", False),
                    "confirm_message": result.get("confirm_message", ""),
                },
            )
        except Exception as e:
            return _err(rid, 5001, str(e))

    if key == "fast":
        raw = str(value or "").strip().lower()
        agent = session.get("agent") if session else None
        if agent is not None:
            current_fast = getattr(agent, "service_tier", None) == "priority"
        else:
            current_fast = _load_service_tier() == "priority"

        if raw in {"status"}:
            return _ok(
                rid,
                {"key": key, "value": "fast" if current_fast else "normal"},
            )

        if raw in {"", "toggle"}:
            nv = "normal" if current_fast else "fast"
        elif raw in {"fast", "on"}:
            nv = "fast"
        elif raw in {"normal", "off"}:
            nv = "normal"
        else:
            return _err(rid, 4002, f"unknown fast mode: {value}")

        overrides = None
        if nv == "fast":
            from hermes_cli.models import resolve_fast_mode_overrides

            target_model = (
                getattr(agent, "model", None) if agent is not None else _resolve_model()
            )
            if not target_model:
                return _err(
                    rid,
                    4002,
                    "fast mode is not available without a selected model",
                )
            overrides = resolve_fast_mode_overrides(target_model)
            if overrides is None:
                return _err(
                    rid,
                    4002,
                    "fast mode is not available for this model",
                )

        _write_config_key("agent.service_tier", nv)
        if agent is not None:
            agent.service_tier = "priority" if nv == "fast" else None
            current_overrides = dict(getattr(agent, "request_overrides", {}) or {})
            current_overrides.pop("service_tier", None)
            current_overrides.pop("speed", None)
            if nv == "fast":
                current_overrides.update(overrides)
            agent.request_overrides = current_overrides
            _emit(
                "session.info",
                params.get("session_id", ""),
                _session_info(agent, session),
            )
        return _ok(rid, {"key": key, "value": nv})

    if key == "busy":
        raw = str(value or "").strip().lower()
        if raw in {"", "status"}:
            return _ok(rid, {"key": key, "value": _load_busy_input_mode()})
        if raw not in {"queue", "steer", "interrupt"}:
            return _err(rid, 4002, f"unknown busy mode: {value}")
        _write_config_key("display.busy_input_mode", raw)
        return _ok(rid, {"key": key, "value": raw})

    if key == "verbose":
        cycle = ["off", "new", "all", "verbose"]
        cur = (
            session.get("tool_progress_mode", _load_tool_progress_mode())
            if session
            else _load_tool_progress_mode()
        )
        if value and value != "cycle":
            nv = str(value).strip().lower()
            if nv not in cycle:
                return _err(rid, 4002, f"unknown verbose mode: {value}")
        else:
            try:
                idx = cycle.index(cur)
            except ValueError:
                idx = 2
            nv = cycle[(idx + 1) % len(cycle)]
        _write_config_key("display.tool_progress", nv)
        if session:
            session["tool_progress_mode"] = nv
            agent = session.get("agent")
            if agent is not None:
                agent.verbose_logging = nv == "verbose"
        return _ok(rid, {"key": key, "value": nv})

    if key == "yolo":
        # Approval bypass. Two scopes:
        #   scope="session" (default) — same as the TUI's Shift+Tab. Toggles
        #     ONLY this session's _session_yolo flag; never touches global
        #     config, so CLI / TUI / cron behavior is unaffected.
        #   scope="global" (Shift+click the zap) — flips the persistent global
        #     approvals.mode in config.yaml between "off" (bypass on) and
        #     "manual" (bypass off). This DOES affect every session, the CLI,
        #     the TUI, and cron, and survives restarts.
        scope = str(params.get("scope") or "session").strip().lower()
        try:
            from tools.approval import (
                disable_session_yolo,
                enable_session_yolo,
                is_session_yolo_enabled,
            )

            raw = str(value or "").strip().lower()

            def _resolve_toggle(current: bool) -> bool:
                if raw in {"1", "on", "true", "yes"}:
                    return True
                if raw in {"0", "off", "false", "no"}:
                    return False
                return not current

            if scope == "global":
                from tools.approval import _normalize_approval_mode

                cfg = _load_cfg()
                appr = cfg.get("approvals") if isinstance(cfg, dict) else None
                if not isinstance(appr, dict):
                    appr = {}
                current = _normalize_approval_mode(appr.get("mode", "manual")) == "off"
                enable = _resolve_toggle(current)
                # Toggle between full bypass and the default manual gate. We do
                # not try to restore a prior "smart"/custom mode — the zap is a
                # binary on/off affordance; users with bespoke modes set them in
                # config.yaml.
                _write_config_key("approvals.mode", "off" if enable else "manual")
                nv = "1" if enable else "0"
                # Reflect the global flip in every live session's indicator.
                for sid, sess in list(_sessions.items()):
                    agent = sess.get("agent")
                    if agent is not None:
                        _emit("session.info", sid, _session_info(agent, sess))
                return _ok(rid, {"key": key, "value": nv, "scope": "global"})

            if session:
                current = is_session_yolo_enabled(session["session_key"])
                enable = _resolve_toggle(current)
                if enable:
                    enable_session_yolo(session["session_key"])
                    nv = "1"
                else:
                    disable_session_yolo(session["session_key"])
                    nv = "0"
                agent = session.get("agent")
                if agent is not None:
                    _emit(
                        "session.info",
                        params.get("session_id", ""),
                        _session_info(agent, session),
                    )
            else:
                current = is_truthy_value(os.environ.get("HERMES_YOLO_MODE"))
                enable = _resolve_toggle(current)
                if enable:
                    os.environ["HERMES_YOLO_MODE"] = "1"
                    nv = "1"
                else:
                    os.environ.pop("HERMES_YOLO_MODE", None)
                    nv = "0"
            return _ok(rid, {"key": key, "value": nv, "scope": "session"})
        except Exception as e:
            return _err(rid, 5001, str(e))

    if key == "reasoning":
        try:
            from hermes_constants import parse_reasoning_effort

            arg = str(value or "").strip().lower()
            if arg in {"show", "on"}:
                cfg = _load_cfg()
                display = (
                    cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
                )
                sections = (
                    display.get("sections")
                    if isinstance(display.get("sections"), dict)
                    else {}
                )
                display["show_reasoning"] = True
                sections["thinking"] = "expanded"
                display["sections"] = sections
                cfg["display"] = display
                _save_cfg(cfg)
                if session:
                    session["show_reasoning"] = True
                return _ok(rid, {"key": key, "value": "show"})
            if arg in {"hide", "off"}:
                cfg = _load_cfg()
                display = (
                    cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
                )
                sections = (
                    display.get("sections")
                    if isinstance(display.get("sections"), dict)
                    else {}
                )
                display["show_reasoning"] = False
                sections["thinking"] = "hidden"
                display["sections"] = sections
                cfg["display"] = display
                _save_cfg(cfg)
                if session:
                    session["show_reasoning"] = False
                return _ok(rid, {"key": key, "value": "hide"})

            parsed = parse_reasoning_effort(arg)
            if parsed is None:
                return _err(rid, 4002, f"unknown reasoning value: {value}")
            _write_config_key("agent.reasoning_effort", arg)
            if session and session.get("agent") is not None:
                session["agent"].reasoning_config = parsed
            return _ok(rid, {"key": key, "value": arg})
        except Exception as e:
            return _err(rid, 5001, str(e))

    if key == "details_mode":
        nv = str(value or "").strip().lower()
        if nv not in _DETAIL_MODES:
            return _err(rid, 4002, f"unknown details_mode: {value}")
        cfg = _load_cfg()
        display = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
        sections = (
            display.get("sections") if isinstance(display.get("sections"), dict) else {}
        )
        display["details_mode"] = nv
        for section in _DETAIL_SECTION_NAMES:
            sections[section] = nv
        display["sections"] = sections
        cfg["display"] = display
        _save_cfg(cfg)
        return _ok(rid, {"key": key, "value": nv})

    if key.startswith("details_mode."):
        # Per-section override: `details_mode.<section>` writes to
        # `display.sections.<section>`. Empty value clears the explicit
        # override and lets frontend resolution apply built-in section defaults
        # before the global details_mode.
        section = key.split(".", 1)[1]
        if section not in _DETAIL_SECTION_NAMES:
            return _err(rid, 4002, f"unknown section: {section}")

        cfg = _load_cfg()
        display = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
        sections_cfg = (
            display.get("sections") if isinstance(display.get("sections"), dict) else {}
        )

        nv = str(value or "").strip().lower()
        if not nv:
            sections_cfg.pop(section, None)
            display["sections"] = sections_cfg
            cfg["display"] = display
            _save_cfg(cfg)
            return _ok(rid, {"key": key, "value": ""})

        if nv not in _DETAIL_MODES:
            return _err(rid, 4002, f"unknown details_mode: {value}")

        sections_cfg[section] = nv
        display["sections"] = sections_cfg
        cfg["display"] = display
        _save_cfg(cfg)
        return _ok(rid, {"key": key, "value": nv})

    if key == "thinking_mode":
        nv = str(value or "").strip().lower()
        allowed_tm = frozenset({"collapsed", "truncated", "full"})
        if nv not in allowed_tm:
            return _err(rid, 4002, f"unknown thinking_mode: {value}")
        _write_config_key("display.thinking_mode", nv)
        # Backward compatibility bridge: keep details_mode aligned.
        _write_config_key(
            "display.details_mode", "expanded" if nv == "full" else "collapsed"
        )
        return _ok(rid, {"key": key, "value": nv})

    if key == "compact":
        raw = str(value or "").strip().lower()
        cfg0 = _load_cfg()
        d0 = cfg0.get("display") if isinstance(cfg0.get("display"), dict) else {}
        cur_b = bool(d0.get("tui_compact", False))
        if raw in {"", "toggle"}:
            nv_b = not cur_b
        elif raw == "on":
            nv_b = True
        elif raw == "off":
            nv_b = False
        else:
            return _err(rid, 4002, f"unknown compact value: {value}")
        _write_config_key("display.tui_compact", nv_b)
        return _ok(rid, {"key": key, "value": "on" if nv_b else "off"})

    if key == "statusbar":
        raw = str(value or "").strip().lower()
        display = _load_cfg().get("display")
        d0 = display if isinstance(display, dict) else {}
        current = _coerce_statusbar(d0.get("tui_statusbar", "top"))

        if raw in {"", "toggle"}:
            nv = "top" if current == "off" else "off"
        elif raw == "on":
            nv = "top"
        elif raw in _STATUSBAR_MODES:
            nv = raw
        else:
            return _err(rid, 4002, f"unknown statusbar value: {value}")

        _write_config_key("display.tui_statusbar", nv)
        return _ok(rid, {"key": key, "value": nv})

    if key == "mouse":
        # Explicit None check rather than `value or ""` so falsy non-string
        # inputs (0, False) reach the alias map as themselves — both map to
        # 'off' via _MOUSE_TRACKING_ALIASES — instead of being collapsed to
        # '' and triggering the toggle path. The slash command always passes
        # a string, but programmatic JSON-RPC callers may send booleans.
        raw = ("" if value is None else str(value)).strip().lower()
        cfg = _load_cfg()
        display = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
        current = _display_mouse_tracking(display)

        if raw in {"", "toggle"}:
            nv = "all" if current == "off" else "off"
        elif raw in _MOUSE_TRACKING_ALIASES:
            nv = _MOUSE_TRACKING_ALIASES[raw]
        else:
            return _err(rid, 4002, f"unknown mouse value: {value}")

        _write_config_key("display.mouse_tracking", nv)
        return _ok(rid, {"key": key, "value": nv})

    if key == "indicator":
        # Use an explicit None check rather than `value or ""` so falsy
        # non-string inputs (0, False, []) still surface as themselves
        # in the error message instead of looking like a blank value.
        raw = ("" if value is None else str(value)).strip().lower()
        if raw not in _INDICATOR_STYLES:
            return _err(
                rid,
                4002,
                f"unknown indicator: {raw!r}; pick one of {'|'.join(_INDICATOR_STYLES)}",
            )
        _write_config_key("display.tui_status_indicator", raw)
        return _ok(rid, {"key": key, "value": raw})

    if key in {"cwd", "terminal.cwd", "workdir"}:
        raw = str(value or "").strip()
        if not raw:
            return _err(rid, 4002, "cwd required")
        cwd = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(cwd):
            return _err(rid, 4002, f"working directory does not exist: {raw}")
        _write_config_key("terminal.cwd", cwd)
        os.environ["TERMINAL_CWD"] = cwd
        return _ok(
            rid,
            {"key": "terminal.cwd", "value": cwd, "cwd": cwd, "branch": _git_branch_for_cwd(cwd)},
        )

    if key in {"prompt", "personality", "skin"}:
        try:
            cfg = _load_cfg()
            if key == "prompt":
                if value == "clear":
                    cfg.pop("custom_prompt", None)
                    nv = ""
                else:
                    cfg["custom_prompt"] = value
                    nv = value
                _save_cfg(cfg)
            elif key == "personality":
                sid_key = params.get("session_id", "")
                pname, new_prompt = _validate_personality(str(value or ""), cfg)
                _write_config_key("display.personality", pname)
                _write_config_key("agent.system_prompt", new_prompt)
                nv = str(value or "none")
                history_reset, info = _apply_personality_to_session(
                    sid_key, session, new_prompt, pname
                )
            else:
                _write_config_key(f"display.{key}", value)
                nv = value
                if key == "skin":
                    _emit("skin.changed", "", resolve_skin())
            resp = {"key": key, "value": nv}
            if key == "personality":
                resp["history_reset"] = history_reset
                if info is not None:
                    resp["info"] = info
            return _ok(rid, resp)
        except Exception as e:
            return _err(rid, 5001, str(e))

    return _err(rid, 4002, f"unknown config key: {key}")


@method("config.get")
def _(rid, params: dict) -> dict:
    key = params.get("key", "")
    if key == "provider":
        try:
            from hermes_cli.models import list_available_providers, normalize_provider

            model = _resolve_model()
            parts = model.split("/", 1)
            return _ok(
                rid,
                {
                    "model": model,
                    "provider": (
                        normalize_provider(parts[0]) if len(parts) > 1 else "unknown"
                    ),
                    "providers": list_available_providers(),
                },
            )
        except Exception as e:
            return _err(rid, 5013, str(e))
    if key == "profile":
        from hermes_constants import display_hermes_home

        return _ok(rid, {"home": str(_hermes_home), "display": display_hermes_home()})
    if key == "project":
        cfg_terminal = _load_cfg().get("terminal") or {}
        raw = str(params.get("cwd", "") or cfg_terminal.get("cwd", "") or "").strip()
        cwd = _completion_cwd({"cwd": raw} if raw else {})
        return _ok(rid, {"cwd": cwd, "branch": _git_branch_for_cwd(cwd)})
    if key == "full":
        return _ok(rid, {"config": _load_cfg()})
    if key == "prompt":
        return _ok(rid, {"prompt": _load_cfg().get("custom_prompt", "")})
    if key == "skin":
        return _ok(
            rid, {"value": (_load_cfg().get("display") or {}).get("skin", "default")}
        )
    if key == "indicator":
        # Normalize so a hand-edited config.yaml with stray casing or
        # an unknown value reads back the SAME value the TUI actually
        # rendered (frontend's `normalizeIndicatorStyle` falls back to
        # `_INDICATOR_DEFAULT` for the same inputs).  Otherwise
        # `/indicator` would print one thing while the UI shows another.
        raw = (_load_cfg().get("display") or {}).get("tui_status_indicator", "")
        norm = str(raw).strip().lower()
        return _ok(
            rid,
            {"value": norm if norm in _INDICATOR_STYLES else _INDICATOR_DEFAULT},
        )
    if key == "personality":
        return _ok(
            rid,
            {"value": (_load_cfg().get("display") or {}).get("personality") or "none"},
        )
    if key == "reasoning":
        cfg = _load_cfg()
        effort = str(
            (cfg.get("agent") or {}).get("reasoning_effort", "medium") or "medium"
        )
        display = (
            "show"
            if bool((cfg.get("display") or {}).get("show_reasoning", False))
            else "hide"
        )
        return _ok(rid, {"value": effort, "display": display})
    if key == "fast":
        return _ok(
            rid,
            {
                "value": (
                    "fast"
                    if (session := _sessions.get(params.get("session_id", "")))
                    and getattr(session.get("agent"), "service_tier", None)
                    == "priority"
                    else ("fast" if _load_service_tier() == "priority" else "normal")
                ),
            },
        )
    if key == "busy":
        return _ok(rid, {"value": _load_busy_input_mode()})
    if key == "details_mode":
        allowed_dm = frozenset({"hidden", "collapsed", "expanded"})
        raw = (
            str(
                (_load_cfg().get("display") or {}).get("details_mode", "collapsed")
                or "collapsed"
            )
            .strip()
            .lower()
        )
        nv = raw if raw in allowed_dm else "collapsed"
        return _ok(rid, {"value": nv})
    if key == "thinking_mode":
        allowed_tm = frozenset({"collapsed", "truncated", "full"})
        cfg = _load_cfg()
        raw = (
            str((cfg.get("display") or {}).get("thinking_mode", "") or "")
            .strip()
            .lower()
        )
        if raw in allowed_tm:
            nv = raw
        else:
            dm = (
                str(
                    (cfg.get("display") or {}).get("details_mode", "collapsed")
                    or "collapsed"
                )
                .strip()
                .lower()
            )
            nv = "full" if dm == "expanded" else "collapsed"
        return _ok(rid, {"value": nv})
    if key == "compact":
        on = bool((_load_cfg().get("display") or {}).get("tui_compact", False))
        return _ok(rid, {"value": "on" if on else "off"})
    if key == "statusbar":
        display = _load_cfg().get("display")
        raw = (
            display.get("tui_statusbar", "top") if isinstance(display, dict) else "top"
        )
        return _ok(rid, {"value": _coerce_statusbar(raw)})
    if key == "mouse":
        display = _load_cfg().get("display")
        return _ok(rid, {"value": _display_mouse_tracking(display)})
    if key == "mtime":
        cfg_path = _hermes_home / "config.yaml"
        try:
            return _ok(
                rid, {"mtime": cfg_path.stat().st_mtime if cfg_path.exists() else 0}
            )
        except Exception:
            return _ok(rid, {"mtime": 0})
    return _err(rid, 4002, f"unknown config key: {key}")


@method("setup.status")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.main import _has_any_provider_configured

        return _ok(rid, {"provider_configured": bool(_has_any_provider_configured())})
    except Exception as e:
        return _err(rid, 5016, str(e))


@method("setup.runtime_check")
def _(rid, params: dict) -> dict:
    """Strict provider check: does the configured/default model actually resolve to a usable runtime?

    Unlike setup.status (which returns True if ANY provider auth state is
    discoverable, including indirect fallbacks like ``gh auth token`` for
    Copilot), this runs the same resolve_runtime_provider() call the agent
    uses on session creation. It returns ok=False with the auth error message
    when the user's configured model cannot actually be served, so UIs can
    surface onboarding before the user submits a doomed prompt.
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.auth import has_usable_secret
        from hermes_cli.main import _has_any_provider_configured

        runtime = resolve_runtime_provider(requested=None)
        provider_configured = bool(_has_any_provider_configured())
        provider = runtime.get("provider") or "provider"
        source = str(runtime.get("source") or "")
        if not provider_configured and provider == "bedrock" and source in {
            "iam-role",
            "aws-sdk-default-chain",
        }:
            return _ok(
                rid,
                {
                    "ok": False,
                    "provider": provider,
                    "model": runtime.get("model"),
                    "source": source,
                    "error": "No Hermes provider is configured.",
                },
            )

        api_key = runtime.get("api_key")
        api_key_text = "" if callable(api_key) else str(api_key or "").strip()
        credential_ok = (
            callable(api_key)
            or api_key_text in {"aws-sdk", "no-key-required"}
            or has_usable_secret(api_key_text)
            or bool(runtime.get("command"))
        )

        if not credential_ok:
            return _ok(
                rid,
                {
                    "ok": False,
                    "provider": provider,
                    "model": runtime.get("model"),
                    "source": runtime.get("source"),
                    "error": f"No usable credentials found for {provider}.",
                },
            )

        return _ok(
            rid,
            {
                "ok": True,
                "provider": runtime.get("provider"),
                "model": runtime.get("model"),
                "source": runtime.get("source"),
            },
        )
    except Exception as e:
        return _ok(rid, {"ok": False, "error": str(e)})


# ── Methods: tools & system ──────────────────────────────────────────


@method("process.stop")
def _(rid, params: dict) -> dict:
    try:
        from tools.process_registry import process_registry

        return _ok(rid, {"killed": process_registry.kill_all()})
    except Exception as e:
        return _err(rid, 5010, str(e))


def _session_processes(session: dict) -> list:
    """Background processes owned by this session (registry session_key match)."""
    from tools.process_registry import process_registry

    key = str(session.get("session_key") or "")
    owned = []
    for entry in process_registry.list_sessions():
        proc = process_registry.get(entry["session_id"])
        if proc is None or str(getattr(proc, "session_key", "") or "") != key:
            continue
        # The 200-char list preview is too thin for the desktop's inline
        # terminal viewer — ship a real tail alongside it.
        entry["output_tail"] = (proc.output_buffer or "")[-4000:]
        owned.append(entry)
    return owned


@method("process.list")
def _(rid, params: dict) -> dict:
    """Session-scoped view of the background process registry (desktop status stack)."""
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        return _ok(rid, {"processes": _session_processes(session)})
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("process.kill")
def _(rid, params: dict) -> dict:
    """Kill ONE background process — scoped to the caller's session so one
    window can't reap another session's work (unlike process.stop's kill_all)."""
    session, err = _sess(params, rid)
    if err:
        return err
    proc_id = str(params.get("process_id") or "")
    if not proc_id:
        return _err(rid, 4012, "process_id required")
    try:
        from tools.process_registry import process_registry

        proc = process_registry.get(proc_id)
        if proc is None or str(getattr(proc, "session_key", "") or "") != str(
            session.get("session_key") or ""
        ):
            return _err(rid, 4044, f"no such process: {proc_id}")
        return _ok(rid, process_registry.kill_process(proc_id))
    except Exception as e:
        return _err(rid, 5010, str(e))


@method("reload.mcp")
def _(rid, params: dict) -> dict:
    session = _sessions.get(params.get("session_id", ""))
    try:
        # Gate: /reload-mcp invalidates the prompt cache for this session.
        # Respect the ``approvals.mcp_reload_confirm`` config toggle — if
        # set (default true) AND the caller did not pass ``confirm=true``
        # in params, surface a warning to the transcript instead of just
        # reloading silently.  Users pass confirm=true either by
        # re-invoking after reading the warning, or by setting the
        # config key to false permanently.
        user_confirm = bool(params.get("confirm", False))
        if not user_confirm:
            try:
                from hermes_cli.config import load_config as _load_config

                _cfg = _load_config()
                _approvals = _cfg.get("approvals") if isinstance(_cfg, dict) else None
                _confirm_required = True
                if isinstance(_approvals, dict):
                    _confirm_required = bool(_approvals.get("mcp_reload_confirm", True))
            except Exception:
                _confirm_required = True
            if _confirm_required:
                # Return a structured response the Ink client can surface
                # as a warning/confirmation without actually reloading yet.
                # Ink's ops.ts reads ``status`` and prints ``message`` to
                # the transcript; a follow-up invocation with confirm=true
                # (or an `always` choice that flips the config) proceeds.
                return _ok(
                    rid,
                    {
                        "status": "confirm_required",
                        "message": (
                            "⚠️  /reload-mcp invalidates the prompt cache (next "
                            "message re-sends full input tokens). Reply `/reload-mcp "
                            "now` to proceed, or `/reload-mcp always` to proceed and "
                            "silence this prompt permanently."
                        ),
                    },
                )

        from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools

        shutdown_mcp_servers()
        discover_mcp_tools()
        if session:
            agent = session["agent"]
            # Rebuild the cached agent's tool snapshot so the current session
            # picks up added/removed MCP tools without `/new` (which discards
            # history).  The agent snapshots tools once at build and never
            # re-reads the registry, so an explicit rebuild is required here.
            # The user already consented to the prompt-cache invalidation via
            # the confirm gate above.  Mirrors gateway/run.py::_execute_mcp_reload.
            try:
                from tools.mcp_tool import refresh_agent_mcp_tools

                # Explicit reload: re-resolve enabled toolsets so a server the
                # user just enabled in config this session is picked up.
                refresh_agent_mcp_tools(
                    agent,
                    enabled_override=_load_enabled_toolsets(),
                    quiet_mode=True,
                )
            except Exception as _exc:
                logger.warning(
                    "Failed to refresh cached agent tools after /reload-mcp: %s",
                    _exc,
                )
            _emit(
                "session.info",
                params.get("session_id", ""),
                _session_info(agent, session),
            )

        # Honor `always=true` by persisting the opt-out to config.
        if bool(params.get("always", False)):
            try:
                from cli import save_config_value as _save_cfg

                _save_cfg("approvals.mcp_reload_confirm", False)
            except Exception as _exc:
                logger.warning("Failed to persist mcp_reload_confirm=false: %s", _exc)

        return _ok(rid, {"status": "reloaded"})
    except Exception as e:
        return _err(rid, 5015, str(e))


@method("reload.env")
def _(rid, params: dict) -> dict:
    """Re-read ``~/.hermes/.env`` into the gateway process via
    ``hermes_cli.config.reload_env``, matching classic CLI's ``/reload``
    handler.  Newly added API keys take effect on the next agent call
    without restarting the TUI.

    The credential pool / provider routing for any *already-constructed*
    agent does not auto-rebuild — that's the same behaviour as classic
    CLI's ``/reload``.  Users who want a brand-new credential resolution
    should follow with ``/new``.
    """
    try:
        from hermes_cli.config import reload_env

        count = reload_env()
        return _ok(rid, {"updated": int(count)})
    except Exception as e:
        return _err(rid, 5015, str(e))


_TUI_HIDDEN: frozenset[str] = frozenset(
    {
        "sethome",
        "set-home",
        "commands",
        "approve",
        "deny",
    }
)

_TUI_EXTRA: list[tuple[str, str, str]] = [
    ("/compact", "Toggle compact display mode", "TUI"),
    ("/logs", "Show recent gateway log lines", "TUI"),
    (
        "/mouse",
        "Set mouse tracking preset [on|off|toggle|wheel|buttons|all]",
        "TUI",
    ),
]

# Commands that queue messages onto _pending_input in the CLI.
# In the TUI the slash worker subprocess has no reader for that queue,
# so slash.exec rejects them → TUI falls through to command.dispatch.
_PENDING_INPUT_COMMANDS: frozenset[str] = frozenset(
    {
        "retry",
        "queue",
        "q",
        "steer",
        "plan",
        "goal",
        "undo",
        "rewind",
    }
)

_WORKER_BLOCKED_COMMANDS: frozenset[str] = frozenset({"snapshot", "snap"})


def _cli_exec_blocked(argv: list[str]) -> str | None:
    """Return user hint if this argv must not run headless in the gateway process."""
    if not argv:
        return "bare `hermes` is interactive — use `/hermes chat -q …` or run `hermes` in another terminal"
    a0 = argv[0].lower()
    if a0 == "setup":
        return "`hermes setup` needs a full terminal — run it outside the TUI"
    if a0 == "gateway":
        return "`hermes gateway` is long-running — run it in another terminal"
    if a0 == "sessions" and len(argv) > 1 and argv[1].lower() == "browse":
        return "`hermes sessions browse` is interactive — use /resume here, or run browse in another terminal"
    if a0 == "config" and len(argv) > 1 and argv[1].lower() == "edit":
        return "`hermes config edit` needs $EDITOR in a real terminal"
    return None


def _resolve_name(name: str) -> str:
    try:
        from hermes_cli.commands import resolve_command

        r = resolve_command(name)
        return r.name if r else name
    except Exception:
        return name


# ── Methods: paste ────────────────────────────────────────────────────

_paste_counter = 0


# ── Methods: slash.exec ──────────────────────────────────────────────


def _mirror_slash_side_effects(sid: str, session: dict, command: str) -> str:
    """Apply side effects that must also hit the gateway's live agent."""
    parts = command.lstrip("/").split(None, 1)
    if not parts:
        return ""
    name, arg, agent = (
        parts[0],
        (parts[1].strip() if len(parts) > 1 else ""),
        session.get("agent"),
    )

    # Reject agent-mutating commands during an in-flight turn.  These
    # all do read-then-mutate on live agent/session state that the
    # worker thread running agent.run_conversation is using.  Parity
    # with the session.compress / session.undo guards and the gateway
    # runner's running-agent /model guard.
    _MUTATES_WHILE_RUNNING = {"model", "personality", "prompt", "compress"}
    if name in _MUTATES_WHILE_RUNNING and session.get("running"):
        return f"session busy — /interrupt the current turn before running /{name}"

    try:
        if name == "model" and arg and agent:
            result = _apply_model_switch(sid, session, arg)
            return result.get("warning", "")
        elif name == "personality" and arg and agent:
            _, new_prompt = _validate_personality(arg, _load_cfg())
            _apply_personality_to_session(sid, session, new_prompt)
        elif name == "prompt" and agent:
            cfg = _load_cfg()
            new_prompt = (cfg.get("agent") or {}).get("system_prompt", "") or ""
            agent.ephemeral_system_prompt = new_prompt or None
            agent._cached_system_prompt = None
        elif name == "compress" and agent:
            _compress_session_history(session, arg)
            _sync_session_key_after_compress(sid, session)
            _emit("session.info", sid, _session_info(agent, session))
        elif name == "fast" and agent:
            mode = arg.lower()
            if mode in {"fast", "on"}:
                agent.service_tier = "priority"
            elif mode in {"normal", "off"}:
                agent.service_tier = None
            _emit("session.info", sid, _session_info(agent, session))
        elif name == "reload-mcp" and agent and hasattr(agent, "reload_mcp_tools"):
            agent.reload_mcp_tools()
        elif name == "stop":
            from tools.process_registry import process_registry

            process_registry.kill_all()
    except Exception as e:
        return f"live session sync failed: {e}"
    return ""


# ── Methods: voice ───────────────────────────────────────────────────


_voice_event_sid: str = ""


# ── Methods: insights ────────────────────────────────────────────────


# ── Methods: rollback ────────────────────────────────────────────────


# ── Methods: browser / plugins / cron / skills ───────────────────────


# ── Methods: shell ───────────────────────────────────────────────────


def _register_extracted_method_modules() -> None:
    """Load decomposed method modules so their @method handlers register."""
    import sys

    integrations = sys.modules.get("tui_gateway.methods.integrations")
    injected_integrations = {}
    if integrations is not None:
        for name in ("_load_cfg", "_save_cfg"):
            value = getattr(integrations, name, None)
            if value is not None and type(value).__name__ != "ServerProxy":
                injected_integrations[name] = value

    from tui_gateway.core.method_registration import register_method_modules

    register_method_modules(globals())
    _DOVIE_EXTENSION.register_gateway_methods(_methods)
    integrations = sys.modules.get("tui_gateway.methods.integrations")
    if integrations is not None:
        for name, value in injected_integrations.items():
            setattr(integrations, name, value)


_register_extracted_method_modules()
