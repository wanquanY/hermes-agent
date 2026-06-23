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


def _restart_slash_worker(session: dict):
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
    # session here is a dict reference; look up its sid in _sessions so
    # _attach_worker can verify identity. If we can't find it, the session
    # was already popped and we close the worker directly.
    sid = None
    with _sessions_lock:
        for candidate_sid, candidate in _sessions.items():
            if candidate is session:
                sid = candidate_sid
                break
    if sid is None:
        try:
            new_worker.close()
        except Exception:
            pass
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


def _apply_model_switch(sid: str, session: dict, raw_input: str) -> dict:
    from hermes_cli.model_switch import parse_model_flags, switch_model
    from hermes_cli.runtime_provider import resolve_runtime_provider

    model_input, explicit_provider, persist_global, _force_refresh = parse_model_flags(raw_input)
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
    return {"value": result.new_model, "warning": result.warning_message or ""}


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
            _restart_slash_worker(session)
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
    _restart_slash_worker(session)
    return info


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


# ── Methods: tools & system ──────────────────────────────────────────


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
