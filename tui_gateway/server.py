import atexit
import concurrent.futures
import contextlib
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
from hermes_agent.storage.cli_session_store import open_cli_session_store as _open_cli_session_store
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
    run_control.terminate_run(
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
    """Return the request-scoped control-plane session store.

    Option D keeps one canonical control-plane DB: the Hermes root
    ``state.db``. ``DOVIE_HERMES_CONTROL_HOME`` remains only as an explicit
    test override for that control-plane home; production should not set it.
    """
    return _get_control_plane_db()


def _is_control_plane_stable_session_id(stable_session_id: str) -> bool:
    stable = str(stable_session_id or "").strip()
    return stable.startswith(
        (
            "team:mission-",
            "team-session-team-conversation-",
            "team-conversation-",
            # Group-chat member-chat worker session: the worker runs in its own
            # profile process, but its run-registry / event-stream MUST live in
            # the same control-plane db as the team conversation it mirrors
            # into. Without this, run reservations / terminal events / status
            # lookups split across two databases (control-plane vs profile),
            # and prompt.py's terminalize_if_still_active sees a stale
            # status="running" in one db while the worker already terminalized
            # in the other ("prompt worker terminal event did not close active
            # run"). Routing to the control-plane db keeps both halves in sync.
            "memberchat:",
        )
    )


def _get_control_plane_db(*, use_active_profile: bool = True):
    global _db, _db_error
    # ``use_active_profile`` is retained for older callers, but Option D makes
    # the control plane a root-level singleton. ``DOVIE_HERMES_CONTROL_HOME``
    # is only a testing override for that control-plane home; production should
    # leave it unset so all control reads/writes use root ``state.db``.
    control_home_env = str(os.environ.get("DOVIE_HERMES_CONTROL_HOME") or "").strip()
    process_home = _resolve_home_path(_hermes_home, fallback=_hermes_home)
    if control_home_env:
        control_home = _resolve_home_path(control_home_env, fallback=control_home_env)
    else:
        control_home = process_home
    create_if_missing = _current_method.get("") not in _READ_ONLY_DB_METHODS
    if not create_if_missing and not (control_home / "state.db").exists():
        return None

    if control_home == process_home:
        # Main gateway path: continue using the process-level `_db` slot via
        # the shared session_store helper (its `active_home == default_home`
        # fast path is correct here — the implicit session store also routes to
        # process_home, matching active_home).
        result = _get_session_db_for_home(
            active_home=control_home,
            default_home=control_home,
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

    # Testing override path: open the requested control-plane DB explicitly.
    # This must never be selected from the active profile context.
    home_key = str(control_home)
    cached = _db_by_home.get(home_key)
    if cached is not None:
        return cached
    db_path = control_home / "state.db"
    try:
        ctrl_db = _open_cli_session_store(db_path)
        _db_by_home[home_key] = ctrl_db
        _db_error_by_home.pop(home_key, None)
        return ctrl_db
    except Exception as exc:
        _db_error_by_home[home_key] = str(exc)
        logger.warning(
            "control-plane session store unavailable at %s: %s", db_path, exc,
        )
        return None


def _db_for_stable_session(stable_session_id: str):
    if _is_control_plane_stable_session_id(stable_session_id):
        return _get_control_plane_db(use_active_profile=False)
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


def _attach_worker(sid: str, session: dict, worker) -> bool:
    """Store ``worker`` on ``session`` iff ``sid`` still maps to it.

    Closes the create/close race (C2): between spawning the slash_worker
    subprocess and adding it to ``session["slash_worker"]``, a concurrent
    teardown can pop ``_sessions[sid]``. Without this re-check we'd leak
    the worker subprocess (and its PTY). Caller must already have spawned
    the worker; this function decides whether to keep or close it.

    Returns ``True`` when the worker was attached to the session, ``False``
    when the session was already gone and the worker was closed here as an
    orphan. Callers that own a cleanup path (e.g. ``_build``'s finally) must
    drop their handle on ``False`` so they don't close the same worker twice.

    Call sites: every place that calls ``_SlashWorker(...)`` then assigns
    it to a session dict — line ~945, ~1661 (_restart_slash_worker),
    ~2376 (session.create dovie path).
    """
    with _sessions_lock:
        if _sessions.get(sid) is session:
            session["slash_worker"] = worker
            return True
    # Session was popped concurrently — worker is now an orphan. Close it
    # outside the lock since worker.close() can block on subprocess wait.
    try:
        worker.close()
    except Exception:
        pass
    return False


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
        run_context = session.get("run_context")
        activity_id = str(
            event_payload.get("activity_id")
            or event_payload.get("activityId")
            or getattr(run_context, "activity_id", "")
            or (f"chat:{stable_session_id}" if stable_session_id else "")
        ).strip()
        if stable_session_id:
            params["stored_session_id"] = stable_session_id
        if run_id:
            params["run_id"] = run_id
        if turn_id:
            params["turn_id"] = turn_id
        if runtime_scope_key:
            params["runtime_scope_key"] = runtime_scope_key
        if activity_id:
            params["activity_id"] = activity_id
        if sid:
            params["runtime_session_id"] = sid
        session_transport = session.get("transport")
        context_transport = current_transport()
        direct_transport = session_transport or context_transport or _stdio_transport
        if stable_session_id and (run_id or event == "session.info"):
            event_db = _db_for_stable_session(stable_session_id)
            frame = {
                "type": event,
                "session_id": sid,
                "stored_session_id": stable_session_id,
                "run_id": run_id,
                "turn_id": turn_id,
                "runtime_session_id": sid,
                "runtime_scope_key": runtime_scope_key,
                "activity_id": activity_id,
                "activityId": activity_id,
                "owner_metadata": {
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                },
                "payload": {
                    **event_payload,
                    **({"activity_id": activity_id, "activityId": activity_id} if activity_id else {}),
                },
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


def _emit_approval_request(sid: str, data: dict | None) -> None:
    """Emit an approval request through the shared command-redaction path."""
    payload = dict(data or {})
    if "command" in payload:
        from gateway.run import _redact_approval_command

        payload["command"] = _redact_approval_command(payload.get("command"))
    _emit("approval.request", sid, payload)


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def method(name: str):
    def dec(fn):
        owner_module = str(getattr(fn, "__module__", ""))
        if owner_module.startswith(("tui_gateway.methods.", "hermes_team_mission.gateway.")):
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


def _push_profile_context_for_request(req: dict) -> Any:
    """Phase 2 of sub-sidecar removal: resolve the request's profile
    scope and push it onto the ``current_profile`` ContextVar.

    Returns a token that ``dispatch`` resets in its ``finally`` block,
    or ``None`` when no profile context could be resolved (handlers
    fall back to module-level state, matching today's behavior).

    Resolution order:
      1. ``runtime_scope_key`` / ``runtimeScopeKey`` directly on the
         request params (or inside ``dovie_profile``).
      2. ``agent_profile_id`` → synthesize ``profile:<id>`` (matches
         ``runtime_scope_from_params`` semantics).
      3. ``stored_session_id`` / ``session_id`` → look up cached
         scope mapping; on miss leave the ContextVar unset (rather
         than blocking the dispatch on a DB query).

    Failures here are swallowed — profile context is an OPTIMIZATION
    (Phase 2-3 callers benefit, Phase 1 fallback path still works).
    Never throwing also means tests / CLI tools that don't carry
    profile metadata aren't broken by the new resolver.
    """
    try:
        from tui_gateway.services.runtime_scope import runtime_scope_from_request
        from tui_gateway.services.profile_context import (
            current_profile,
            profile_registry,
            lookup_stable_session_scope,
        )
    except Exception:
        return None
    try:
        scope = runtime_scope_from_request(req)
        scope_key = str(scope.runtime_scope_key or "").strip()
        agent_profile_id = str(scope.agent_profile_id or "").strip()
        if not scope_key and isinstance(req, dict):
            params = req.get("params") if isinstance(req.get("params"), dict) else {}
            stable = str(
                params.get("stored_session_id")
                or params.get("storedSessionId")
                or params.get("session_id")
                or ""
            ).strip()
            if stable:
                scope_key = lookup_stable_session_scope(stable) or ""
        if not scope_key:
            return None
        ctx = profile_registry.get_or_create(scope_key, agent_profile_id=agent_profile_id)
        return current_profile.set(ctx)
    except Exception:
        return None


def _reset_profile_context(token: Any) -> None:
    if token is None:
        return
    try:
        from tui_gateway.services.profile_context import current_profile
        current_profile.reset(token)
    except Exception:
        pass


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
    # Phase 2 — push current_profile ContextVar so per-profile state in
    # tools/approval.py, tools/clarify_gateway.py, etc. routes to the
    # right bucket. Pool handlers (_LONG_HANDLERS path below) propagate
    # the ContextVar automatically via ``contextvars.copy_context()``.
    profile_token = _push_profile_context_for_request(req)
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
        _reset_profile_context(profile_token)
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
                # session between agent build and now. When _attach_worker
                # already closed the worker as an orphan, drop our handle so
                # the finally below doesn't close the same worker a second time.
                if not _attach_worker(sid, current, worker):
                    worker = None
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
                    key, lambda data: _emit_approval_request(sid, data)
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


from tui_gateway.core.session_config import (
    _INDICATOR_DEFAULT,
    _load_cfg,
    _save_cfg,
    _set_session_context,
    _dovie_browser_session_id,
    _clear_session_context,
    _session_cwd,
    _enable_gateway_prompts,
    _block,
    _project_block_state,
    _clear_pending,
    resolve_skin,
    _resolve_model,
    _resolve_startup_runtime,
    _persisted_session_runtime,
    _runtime_model_config,
    _persist_live_session_runtime,
    _persist_live_session_system_prompt,
    _append_model_switch_marker,
    _claim_active_session_slot,
    _ensure_session_db_row,
    _session_db,
    _git_branch_for_cwd,
    _child_run_active,
    _completion_cwd,
    _profile_home,
    _coerce_seed_history,
    _stored_session_runtime_overrides,
    _content_display_text,
    _profile_configured_cwd,
    _inflight_snapshot,
    _register_session_cwd,
    _set_session_cwd,
)
from tui_gateway.core.runtime_settings import (
    _BARE_BILLING_PROVIDERS,
    _CHILD_RUN_STALE_S,
    _CWD_PLACEHOLDERS,
    _session_source,
    _terminal_task_cwd,
    _write_config_key,
    _STATUSBAR_MODES,
    _coerce_statusbar,
    _MOUSE_TRACKING_ALIASES,
    _display_mouse_tracking,
    _load_reasoning_config,
    _load_service_tier,
    _load_show_reasoning,
    _load_tool_progress_mode,
    _load_enabled_toolsets,
    _load_disabled_toolsets,
    _session_tool_progress_mode,
    _session_verbose,
    _tool_progress_enabled,
    _restart_slash_worker,
    _persist_model_switch,
    _apply_model_switch,
    _compress_session_history,
    _sync_session_key_after_compress,
    _current_profile_name,
)
from tui_gateway.core.agent_session import (
    _TUI_VERBOSE_TEXT_MAX_CHARS,
    _TUI_VERBOSE_TEXT_MAX_LINES,
    _cap_tui_verbose_text,
    _redact_tui_verbose_text,
    _tool_args_text,
    _tool_result_text,
    _tool_event_bridge,
    _before_tool_text_boundary,
    _on_tool_start,
    _on_tool_complete,
    _agent_cbs,
    _wire_callbacks,
    _render_personality_prompt,
    _available_personalities,
    _validate_personality,
    _apply_personality_to_session,
    _cfg_max_turns,
    _parse_tui_skills_env,
    _background_agent_kwargs,
    _reset_session_agent,
    _schedule_mcp_late_refresh,
    _make_agent,
    _init_session,
)

_register_extracted_method_modules()
