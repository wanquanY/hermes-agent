import atexit
import concurrent.futures
import contextvars
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

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_cli.env_loader import load_hermes_dotenv
from utils import is_truthy_value
from tui_gateway.transport import (
    StdioTransport,
    Transport,
    bind_transport,
    current_transport,
    reset_transport,
)
from tui_gateway.services.artifacts import record_artifacts_from_tool_complete
from tui_gateway.services.config_store import (
    load_cfg as _load_cfg_from_store,
    save_cfg as _save_cfg_to_store,
)
from tui_gateway.services.display_config import (
    coerce_statusbar as _coerce_statusbar,
    display_mouse_tracking as _display_mouse_tracking,
    load_busy_input_mode as _load_busy_input_mode_from_config,
)
from tui_gateway.services.media import (
    estimate_image_tokens as _estimate_image_tokens,
    image_meta as _image_meta,
)
from tui_gateway.services.session_lifecycle import (
    finalize_session as _finalize_session_impl,
    notify_session_boundary as _notify_session_boundary_impl,
    shutdown_sessions as _shutdown_sessions_impl,
)
from tui_gateway.services.completions import (
    details_completions as _details_completions,
    fuzzy_basename_rank as _fuzzy_basename_rank,
    fuzzy_cache as _fuzzy_cache,
    list_repo_files as _list_repo_files,
    normalize_completion_path as _normalize_completion_path,
    path_completion_items,
)
from tui_gateway.services.slash_worker_client import SlashWorker
from tui_gateway.services.workspace import (
    bind_session_workspace as _bind_session_workspace,
    normalize_session_cwd as _normalize_session_cwd,
    session_cwd as _session_cwd,
    workspace_for_session as _workspace_for_session,
    workspace_from_params as _workspace_from_params,
)

logger = logging.getLogger(__name__)

_hermes_home = get_hermes_home()
load_hermes_dotenv(
    hermes_home=_hermes_home, project_env=Path(__file__).parent.parent / ".env"
)


from tui_gateway.core.panic import install_panic_hooks

install_panic_hooks(_hermes_home)
_CRASH_LOG = os.path.join(_hermes_home, "logs", "tui_gateway_crash.log")

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
_db_by_home: dict[str, Any] = {}
_db_error: str | None = None
_profile_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "tui_gateway_profile_context",
    default=None,
)
_profile_env_lock = threading.RLock()
_stdout_lock = threading.Lock()
_DETAIL_SECTION_NAMES = ("thinking", "tools", "subagents", "activity")
_DETAIL_MODES = frozenset({"hidden", "collapsed", "expanded"})
_STATUSBAR_MODES = frozenset({"off", "top", "bottom"})

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
        "platforms.manage",
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

# Backwards-compatible patch point for tests and older gateway extensions.
_SlashWorker = SlashWorker


def _load_busy_input_mode() -> str:
    return _load_busy_input_mode_from_config(_load_cfg)


def _notify_session_boundary(event_type: str, session_id: str | None) -> None:
    _notify_session_boundary_impl(event_type, session_id)


def _finalize_session(session: dict | None, end_reason: str = "tui_close") -> None:
    if session and session.get("transient"):
        session["_finalized"] = True
        return
    _finalize_session_impl(
        session,
        end_reason=end_reason,
        get_db=_get_db,
        notify=_notify_session_boundary,
    )


def _shutdown_sessions() -> None:
    _shutdown_sessions_impl(
        _sessions,
        lambda session: _finalize_session(session, end_reason="tui_shutdown"),
    )


atexit.register(_shutdown_sessions)


# ── Plumbing ──────────────────────────────────────────────────────────


def _active_hermes_home() -> Path:
    profile = _profile_context.get()
    raw = profile.get("hermes_home") if isinstance(profile, dict) else None
    if raw:
        return Path(str(raw)).expanduser().resolve()
    return get_hermes_home()


def _normalize_profile_context(params: dict | None = None) -> dict | None:
    raw = (params or {}).get("doxie_profile") or (params or {}).get("doxieProfile")
    if not isinstance(raw, dict):
        return None
    hermes_home = str(
        raw.get("hermesHomePath")
        or raw.get("hermes_home")
        or raw.get("hermesHome")
        or ""
    ).strip()
    if not hermes_home:
        return None
    env = raw.get("env")
    safe_env = {
        str(k): str(v)
        for k, v in (env.items() if isinstance(env, dict) else [])
        if str(k).strip() and v is not None
    }
    return {
        "id": str(raw.get("id") or raw.get("agentProfileId") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "hermes_home": str(Path(hermes_home).expanduser().resolve()),
        "env": safe_env,
    }


def _profile_context_for_params(params: dict | None = None) -> dict | None:
    explicit = _normalize_profile_context(params)
    if explicit:
        return explicit
    sid = str((params or {}).get("session_id") or "").strip()
    if sid:
        session = _sessions.get(sid)
        stored = session.get("profile_context") if isinstance(session, dict) else None
        if isinstance(stored, dict):
            return stored
    return None


def _enter_profile_context(profile: dict | None):
    if not isinstance(profile, dict) or not profile.get("hermes_home"):
        return []
    env = profile.get("env") if isinstance(profile.get("env"), dict) else {}
    _profile_env_lock.acquire()
    previous_env = {key: os.environ.get(key) for key in env}
    for key, value in env.items():
        os.environ[str(key)] = str(value)
    home_token = set_hermes_home_override(profile["hermes_home"])
    context_token = _profile_context.set(profile)
    return [context_token, home_token, previous_env]


def _leave_profile_context(tokens: list) -> None:
    if not tokens:
        return
    context_token, home_token, previous_env = tokens
    try:
        _profile_context.reset(context_token)
        reset_hermes_home_override(home_token)
        for key, previous in previous_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    finally:
        _profile_env_lock.release()


def _get_db():
    global _db_error
    hermes_home = _active_hermes_home()
    key = str(hermes_home)
    if key not in _db_by_home:
        from hermes_state import SessionDB

        try:
            _db_by_home[key] = SessionDB(hermes_home / "state.db")
            _db_error = None
        except Exception as exc:
            _db_error = str(exc)
            logger.warning(
                "TUI session store unavailable — continuing without state.db features: %s",
                exc,
            )
            return None
    return _db_by_home[key]


def _db_unavailable_error(rid, *, code: int):
    detail = _db_error or "state.db unavailable"
    return _err(rid, code, f"state.db unavailable: {detail}")


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
        if sid and (t := (_sessions.get(sid) or {}).get("transport")) is not None:
            return t.write(obj)

    return (current_transport() or _stdio_transport).write(obj)


def _emit(event: str, sid: str, payload: dict | None = None):
    session = _sessions.get(sid) or {}
    session["event_seq"] = int(session.get("event_seq") or 0) + 1
    session["run_updated_at"] = time.time()
    params = {
        "type": event,
        "session_id": sid,
        "stored_session_id": str(session.get("session_key") or ""),
        "run_id": str(session.get("active_run_id") or session.get("interrupted_run_id") or ""),
        "turn_id": str(session.get("active_turn_id") or session.get("interrupted_turn_id") or ""),
        "seq": int(session.get("event_seq") or 0),
    }
    if payload and payload.get("run_id"):
        params["run_id"] = str(payload.get("run_id") or "")
    if payload and payload.get("turn_id"):
        params["turn_id"] = str(payload.get("turn_id") or "")
    if payload is not None:
        params["payload"] = payload
    write_json({"jsonrpc": "2.0", "method": "event", "params": params})


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
        _methods[name] = fn
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
    if not fn:
        return _err(rid, -32601, f"unknown method: {method}")
    tokens = _enter_profile_context(_profile_context_for_params(params))
    try:
        return fn(rid, params)
    finally:
        _leave_profile_context(tokens)


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

        _rid, method, _params = normalized
        if method not in _LONG_HANDLERS:
            try:
                return handle_request(req)
            except Exception as exc:
                return _err(req.get("id"), -32000, f"handler error: {exc}")

        # Snapshot the context so the pool worker sees the bound transport.
        ctx = contextvars.copy_context()

        def run():
            try:
                resp = handle_request(req)
            except Exception as exc:
                resp = _err(req.get("id"), -32000, f"handler error: {exc}")
            if resp is not None:
                t.write(resp)

        _pool.submit(lambda: ctx.run(run))

        return None
    finally:
        reset_transport(token)


def _wait_agent(session: dict, rid: str, timeout: float = 30.0) -> dict | None:
    ready = session.get("agent_ready")
    if ready is not None and not ready.wait(timeout=timeout):
        return _err(rid, 5032, "agent initialization timed out")
    err = session.get("agent_error")
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
            return
        generation = int(session.get("agent_build_generation") or 0) + 1
        session["agent_build_generation"] = generation
        session["agent_build_started"] = True
    key = session["session_key"]

    def _build() -> None:
        current = _sessions.get(sid)
        if current is None:
            ready.set()
            return

        worker = None
        notify_registered = False
        profile_tokens = _enter_profile_context(current.get("profile_context"))
        try:
            tokens = _set_session_context(key, terminal_cwd=current.get("cwd"))
            try:
                agent = _make_agent(sid, key, cwd=current.get("cwd"))
            finally:
                _clear_session_context(tokens)

            if (
                _sessions.get(sid) is not current
                or int(current.get("agent_build_generation") or 0) != generation
            ):
                return

            # Session DB row deferred to first run_conversation() call.
            # pending_title applied post-first-message (see cli.exec handler).
            _install_agent_after_pending_model_switch(sid, current, agent)

            try:
                worker = _SlashWorker(key, getattr(agent, "model", _resolve_model()))
                current["slash_worker"] = worker
            except Exception:
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

            _wire_callbacks(sid)
            _notify_session_boundary("on_session_reset", key)

            info = _session_info(agent, current)
            warn = _probe_credentials(agent)
            if warn:
                info["credential_warning"] = warn
            cfg_warn = _probe_config_health(_load_cfg())
            if cfg_warn:
                info["config_warning"] = cfg_warn
                logger.warning(cfg_warn)
            _emit("session.info", sid, info)
        except Exception as e:
            if int(current.get("agent_build_generation") or 0) == generation:
                current["agent_error"] = str(e)
                _emit("error", sid, {"message": f"agent init failed: {e}"})
        finally:
            stale_build = (
                _sessions.get(sid) is not current
                or int(current.get("agent_build_generation") or 0) != generation
            )
            if stale_build:
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
            _leave_profile_context(profile_tokens)
            if not stale_build:
                ready.set()

    threading.Thread(target=_build, daemon=True).start()


def _runtime_session_candidates(session_id: str):
    target = str(session_id or "").strip()
    if not target:
        return []
    exact = _sessions.get(target)
    if exact is not None:
        return [(target, exact)]
    matches = []
    try:
        snapshot = list(_sessions.items())
    except RuntimeError:
        snapshot = list(_sessions.items())
    for sid, session in snapshot:
        if str((session or {}).get("session_key") or "") == target:
            matches.append((sid, session))
    matches.sort(
        key=lambda item: (
            1 if (item[1] or {}).get("running") else 0,
            float((item[1] or {}).get("run_updated_at") or 0),
            float((item[1] or {}).get("run_started_at") or 0),
        ),
        reverse=True,
    )
    return matches


def _resolve_runtime_session(session_id: str):
    candidates = _runtime_session_candidates(session_id)
    return candidates[0] if candidates else ("", None)


def _sess_nowait(params, rid):
    _sid, s = _resolve_runtime_session(params.get("session_id") or "")
    return (s, None) if s else (None, _err(rid, 4001, "session not found"))


def _sess(params, rid):
    sid, s = _resolve_runtime_session(params.get("session_id") or "")
    if not s:
        err = _err(rid, 4001, "session not found")
        return (None, err)
    _start_agent_build(sid, s)
    return (s, _wait_agent(s, rid))


# ── Config I/O ────────────────────────────────────────────────────────


# Keep aligned with `INDICATOR_STYLES` / `DEFAULT_INDICATOR_STYLE` in
# ``ui-tui/src/app/interfaces.ts`` — both ends validate against the
# same shape so `config.get indicator` and the live TUI render agree.
_INDICATOR_STYLES: tuple[str, ...] = ("ascii", "emoji", "kaomoji", "unicode")
_INDICATOR_DEFAULT = "kaomoji"


def _load_cfg() -> dict:
    return _load_cfg_from_store(_active_hermes_home())


def _save_cfg(cfg: dict):
    _save_cfg_to_store(_active_hermes_home(), cfg)


def _set_session_context(
    session_key: str,
    terminal_cwd: str | None = None,
    doxie_product_context: str = "",
) -> list:
    try:
        from gateway.session_context import set_session_vars

        return set_session_vars(
            session_key=session_key,
            terminal_cwd=_normalize_session_cwd(terminal_cwd),
            doxie_product_context=doxie_product_context,
        )
    except Exception:
        return []


def _clear_session_context(tokens: list) -> None:
    if not tokens:
        return
    try:
        from gateway.session_context import clear_session_vars

        clear_session_vars(tokens)
    except Exception:
        pass


def _enable_gateway_prompts() -> None:
    """Route approvals through gateway callbacks instead of CLI input()."""
    os.environ["HERMES_GATEWAY_SESSION"] = "1"
    os.environ["HERMES_EXEC_ASK"] = "1"
    os.environ["HERMES_INTERACTIVE"] = "1"


# ── Blocking prompt factory ──────────────────────────────────────────


def _block(event: str, sid: str, payload: dict, timeout: int = 300) -> str:
    rid = uuid.uuid4().hex[:8]
    ev = threading.Event()
    _pending[rid] = (sid, ev)
    payload["request_id"] = rid
    _emit(event, sid, payload)
    ev.wait(timeout=timeout)
    _pending.pop(rid, None)
    return _answers.pop(rid, "")


def _clear_pending(sid: str | None = None) -> None:
    """Release pending prompts with an empty answer.

    When *sid* is provided, only prompts owned by that session are
    released — critical for session.interrupt, which must not
    collaterally cancel clarify/sudo/secret prompts on unrelated
    sessions sharing the same tui_gateway process.  When *sid* is
    None, every pending prompt is released (used during shutdown).
    """
    for rid, (owner_sid, ev) in list(_pending.items()):
        if sid is None or owner_sid == sid:
            _answers[rid] = ""
            ev.set()


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


def _requested_tool_progress_mode(params: dict | None = None) -> str:
    if isinstance(params, dict):
        raw = params.get("tool_progress_mode") or params.get("toolProgressMode")
        if raw is not None:
            mode = str(raw or "").strip().lower()
            if mode in {"off", "new", "all", "verbose"}:
                return mode
            if raw is True:
                return "all"
            if raw is False:
                return "off"
    return _load_tool_progress_mode()


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


def _normalize_turn_toolsets(value) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    seen: set[str] = set()
    normalized: list[str] = []
    try:
        from toolsets import validate_toolset
    except Exception:
        validate_toolset = None
    for raw in raw_items:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        if validate_toolset is not None and not validate_toolset(name):
            continue
        seen.add(name)
        normalized.append(name)
    return normalized


def _merge_enabled_toolsets(base: list[str] | None, extra: list[str] | None) -> list[str] | None:
    extra = list(extra or [])
    if not extra:
        return base
    if base is None:
        return None
    merged = list(base)
    seen = set(merged)
    for name in extra:
        if name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


def _session_enabled_toolsets(session: dict | None) -> list[str] | None:
    return _merge_enabled_toolsets(
        _load_enabled_toolsets(),
        _normalize_turn_toolsets((session or {}).get("turn_enabled_toolsets")),
    )


def _ensure_session_turn_toolsets(sid: str, session: dict, enabled_toolsets) -> None:
    requested = _normalize_turn_toolsets(enabled_toolsets)
    current = _normalize_turn_toolsets(session.get("turn_enabled_toolsets"))
    if requested == current:
        return
    session["turn_enabled_toolsets"] = requested
    ready = session.get("agent_ready")
    if ready is not None:
        ready.clear()
    session["agent_build_started"] = False
    session["agent"] = None
    session["agent_error"] = None
    _start_agent_build(sid, session)


def _session_tool_progress_mode(sid: str) -> str:
    return str(_sessions.get(sid, {}).get("tool_progress_mode", "all") or "all")


def _tool_progress_enabled(sid: str) -> bool:
    return _session_tool_progress_mode(sid) != "off"


def _restart_slash_worker(session: dict):
    worker = session.get("slash_worker")
    if worker:
        try:
            worker.close()
        except Exception:
            pass
    try:
        session["slash_worker"] = _SlashWorker(
            session["session_key"],
            getattr(session.get("agent"), "model", _resolve_model()),
        )
    except Exception:
        session["slash_worker"] = None


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


def _apply_model_switch_result_to_agent(agent, result) -> None:
    agent.switch_model(
        new_model=result.new_model,
        new_provider=result.target_provider,
        api_key=result.api_key,
        base_url=result.base_url,
        api_mode=result.api_mode,
    )
    if getattr(result, "target_provider", None):
        setattr(agent, "_gateway_runtime_requested_provider", result.target_provider)


def _ensure_agent_runtime_current(sid: str, session: dict) -> bool:
    """Rebind a live session agent to the current process-level runtime auth.

    The desktop Doxie integration can rotate the cloud proxy token while a TUI
    session object survives in memory.  The gateway process env/config may be
    current, but the already-built AIAgent owns an OpenAI client constructed
    with the old api_key.  Re-resolve runtime credentials before each prompt and
    rebuild the agent client in place when the provider endpoint or credential
    changed.
    """
    from hermes_cli.runtime_provider import resolve_runtime_provider

    lock = session.setdefault("model_switch_lock", threading.Lock())
    with lock:
        agent = session.get("agent")
        if not agent:
            return False

        current_model = getattr(agent, "model", "") or _resolve_model()
        current_provider = getattr(agent, "provider", "") or ""
        current_base_url = getattr(agent, "base_url", "") or ""
        current_api_key = getattr(agent, "api_key", "") or ""
        current_api_mode = getattr(agent, "api_mode", "") or ""
        requested_provider = (
            getattr(agent, "_gateway_runtime_requested_provider", None)
            or None
        )

        runtime = resolve_runtime_provider(
            requested=requested_provider,
            target_model=current_model or None,
        )
        next_provider = str(runtime.get("provider") or current_provider)
        next_base_url = str(runtime.get("base_url") or current_base_url)
        next_api_key = str(runtime.get("api_key") or "")
        next_api_mode = str(runtime.get("api_mode") or current_api_mode)

        changed = (
            next_provider != current_provider
            or next_base_url.rstrip("/") != str(current_base_url).rstrip("/")
            or next_api_mode != current_api_mode
            or (bool(next_api_key) and next_api_key != current_api_key)
        )
        if not changed:
            return False

        agent.switch_model(
            new_model=current_model,
            new_provider=next_provider,
            api_key=next_api_key,
            base_url=next_base_url,
            api_mode=next_api_mode,
        )
        setattr(
            agent,
            "_gateway_runtime_requested_provider",
            runtime.get("requested_provider") or requested_provider or "",
        )
        print(
            "[tui_gateway] rebound session agent runtime credentials before prompt",
            file=sys.stderr,
            flush=True,
        )
        _emit("session.info", sid, _session_info(agent, session))
        return True


def _normalize_model_descriptor(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}

    descriptor: dict[str, Any] = {}
    for key in ("id", "name", "provider", "api_provider", "api_format", "reasoning_format"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            descriptor[key] = value.strip()

    context_window = raw.get("context_window")
    if isinstance(context_window, (int, float)) and context_window > 0:
        descriptor["context_window"] = int(context_window)

    for key in (
        "vision_enabled",
        "reasoning_enabled",
        "reasoning_extra_body_enabled",
        "responses_api_required_for_reasoning_with_tools",
    ):
        if isinstance(raw.get(key), bool):
            descriptor[key] = raw[key]

    request_params = raw.get("request_params")
    if isinstance(request_params, dict):
        descriptor["request_params"] = dict(request_params)

    return descriptor if descriptor.get("id") else {}


def _apply_model_descriptor_to_agent(agent, descriptor: dict) -> None:
    if agent is None:
        return
    setattr(agent, "model_descriptor", dict(descriptor or {}))


def _set_session_model_descriptor(session: dict, descriptor: dict, *, clear_if_empty: bool = False) -> None:
    normalized = _normalize_model_descriptor(descriptor)
    if normalized:
        session["model_descriptor"] = normalized
    elif clear_if_empty:
        session.pop("model_descriptor", None)
    else:
        normalized = dict(session.get("model_descriptor") or {})

    _apply_model_descriptor_to_agent(session.get("agent"), normalized)


def _store_pending_model_switch(session: dict, result) -> None:
    session["pending_model_switch"] = result


def _install_agent_after_pending_model_switch(sid: str, session: dict, agent) -> None:
    lock = session.setdefault("model_switch_lock", threading.Lock())
    with lock:
        pending = session.pop("pending_model_switch", None)
        if pending is not None:
            _apply_model_switch_result_to_agent(agent, pending)
        _apply_model_descriptor_to_agent(agent, dict(session.get("model_descriptor") or {}))
        session["agent"] = agent


def _apply_model_switch(sid: str, session: dict, raw_input: str) -> dict:
    from hermes_cli.model_switch import parse_model_flags, switch_model
    from hermes_cli.runtime_provider import resolve_runtime_provider

    model_input, explicit_provider, persist_global = parse_model_flags(raw_input)
    if not model_input:
        raise ValueError("model value required")

    lock = session.setdefault("model_switch_lock", threading.Lock()) if sid else None
    if lock is not None:
        with lock:
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
                current_api_key = str(runtime.get("api_key", "") or "")
    else:
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
            current_api_key = str(runtime.get("api_key", "") or "")

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

    if lock is not None:
        with lock:
            agent = session.get("agent")
            if agent:
                _apply_model_switch_result_to_agent(agent, result)
                _restart_slash_worker(session)
                _emit("session.info", sid, _session_info(agent, session))
            else:
                _store_pending_model_switch(session, result)
    elif agent:
        _apply_model_switch_result_to_agent(agent, result)
        _restart_slash_worker(session)
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


def _get_usage(agent) -> dict:
    g = lambda k, fb=None: getattr(agent, k, 0) or (getattr(agent, fb, 0) if fb else 0)
    usage = {
        "model": getattr(agent, "model", "") or "",
        "input": g("session_input_tokens", "session_prompt_tokens"),
        "output": g("session_output_tokens", "session_completion_tokens"),
        "cache_read": g("session_cache_read_tokens"),
        "cache_write": g("session_cache_write_tokens"),
        "reasoning": g("session_reasoning_tokens"),
        "prompt": g("session_prompt_tokens"),
        "completion": g("session_completion_tokens"),
        "total": g("session_total_tokens"),
        "calls": g("session_api_calls"),
    }
    comp = getattr(agent, "context_compressor", None)
    if comp:
        ctx_used = getattr(comp, "last_prompt_tokens", 0) or usage["total"] or 0
        ctx_max = getattr(comp, "context_length", 0) or 0
        if ctx_max:
            usage["context_used"] = ctx_used
            usage["context_max"] = ctx_max
            usage["context_percent"] = max(0, min(100, round(ctx_used / ctx_max * 100)))
        usage["compressions"] = getattr(comp, "compression_count", 0) or 0
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

        cost = estimate_usage_cost(
            usage["model"],
            CanonicalUsage(
                input_tokens=usage["input"],
                output_tokens=usage["output"],
                cache_read_tokens=usage["cache_read"],
                cache_write_tokens=usage["cache_write"],
            ),
            provider=getattr(agent, "provider", None),
            base_url=getattr(agent, "base_url", None),
        )
        usage["cost_status"] = cost.status
        if cost.amount_usd is not None:
            usage["cost_usd"] = float(cost.amount_usd)
    except Exception:
        pass
    return usage


def _probe_credentials(agent) -> str:
    """Light credential check at session creation — returns warning or ''."""
    try:
        key = getattr(agent, "api_key", "") or ""
        provider = getattr(agent, "provider", "") or ""
        if not key or key == "no-key-required":
            return f"No API key configured for provider '{provider}'. First message will fail."
    except Exception:
        pass
    return ""


def _probe_config_health(cfg: dict) -> str:
    """Flag bare YAML keys (`agent:` with no value → None) that silently
    drop nested settings. Returns warning or ''."""
    if not isinstance(cfg, dict):
        return ""
    warnings: list[str] = []
    null_keys = sorted(k for k, v in cfg.items() if v is None)
    if not null_keys:
        pass
    else:
        keys = ", ".join(f"`{k}`" for k in null_keys)
        warnings.append(
            f"config.yaml has empty section(s): {keys}. "
            f"Remove the line(s) or set them to `{{}}` — "
            f"empty sections silently drop nested settings."
        )
    display_cfg = cfg.get("display")
    agent_cfg = cfg.get("agent")
    if isinstance(display_cfg, dict):
        personality = str(display_cfg.get("personality", "") or "").strip().lower()
        if (
            personality
            and personality not in {"default", "none", "neutral"}
            and isinstance(agent_cfg, dict)
            and agent_cfg.get("personalities") is None
        ):
            warnings.append(
                "`display.personality` is set but `agent.personalities` is empty/null; "
                "personality overlay will be skipped."
            )
    return " ".join(warnings).strip()


def _session_info(agent, session: dict | None = None) -> dict:
    reasoning_config = getattr(agent, "reasoning_config", None)
    reasoning_effort = ""
    if (
        isinstance(reasoning_config, dict)
        and reasoning_config.get("enabled") is not False
    ):
        reasoning_effort = str(reasoning_config.get("effort", "") or "")
    service_tier = getattr(agent, "service_tier", None) or ""
    info: dict = {
        "model": getattr(agent, "model", ""),
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "fast": service_tier == "priority",
        "tools": {},
        "skills": {},
        "cwd": _session_cwd(session),
        "workspace": dict(session.get("workspace") or {}) if session else {},
        "model_descriptor": dict(session.get("model_descriptor") or {}) if session else {},
        "version": "",
        "release_date": "",
        "update_behind": None,
        "update_command": "",
        "usage": _get_usage(agent),
    }
    try:
        from hermes_cli import __version__, __release_date__

        info["version"] = __version__
        info["release_date"] = __release_date__
    except Exception:
        pass
    try:
        from model_tools import get_toolset_for_tool

        for t in getattr(agent, "tools", []) or []:
            name = t["function"]["name"]
            info["tools"].setdefault(get_toolset_for_tool(name) or "other", []).append(
                name
            )
    except Exception:
        pass
    try:
        from hermes_cli.banner import get_available_skills

        info["skills"] = get_available_skills()
    except Exception:
        pass
    try:
        from tools.mcp_tool import get_mcp_status

        info["mcp_servers"] = get_mcp_status()
    except Exception:
        info["mcp_servers"] = []
    try:
        info["system_prompt"] = getattr(agent, "_cached_system_prompt", "") or ""
    except Exception:
        pass
    try:
        from hermes_cli.banner import get_update_result
        from hermes_cli.config import recommended_update_command

        info["update_behind"] = get_update_result(timeout=0.5)
        info["update_command"] = recommended_update_command()
    except Exception:
        pass
    return info


def _tool_ctx(name: str, args: dict) -> str:
    try:
        from agent.display import build_tool_preview

        return build_tool_preview(name, args, max_len=80) or ""
    except Exception:
        return ""


def _tool_args_payload(args: dict | None) -> dict:
    if not isinstance(args, dict):
        return {}
    try:
        json.dumps(args)
        return args
    except Exception:
        return {
            str(key): str(value)
            for key, value in args.items()
            if isinstance(key, (str, int, float, bool))
        }


def _fmt_tool_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{round(seconds)}s"
    mins, secs = divmod(int(round(seconds)), 60)
    return f"{mins}m {secs}s" if secs else f"{mins}m"


def _count_list(obj: object, *path: str) -> int | None:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return len(cur) if isinstance(cur, list) else None


def _tool_summary(name: str, result: str, duration_s: float | None) -> str | None:
    try:
        data = json.loads(result)
    except Exception:
        data = None

    dur = _fmt_tool_duration(duration_s)
    suffix = f" in {dur}" if dur else ""
    text = None

    if name == "web_search" and isinstance(data, dict):
        n = _count_list(data, "data", "web")
        if n is not None:
            text = f"Did {n} {'search' if n == 1 else 'searches'}"

    elif name == "web_extract" and isinstance(data, dict):
        n = _count_list(data, "results") or _count_list(data, "data", "results")
        if n is not None:
            text = f"Extracted {n} {'page' if n == 1 else 'pages'}"

    if isinstance(data, dict) and data.get("fallback_warning"):
        warning = str(data.get("fallback_warning") or "").strip()
        if warning:
            return f"{warning}{suffix}"

    return f"{text}{suffix}" if text else None


_DOXIE_STRUCTURED_RESULT_TOOLS = {
    "design_agent_profile",
    "create_agent_profile_draft",
    "create_agent_profile_revision_draft",
    "doxie_agent_profile_create_draft",
    "test_agent_profile",
}


def _doxie_structured_tool_result(name: str, result: str) -> dict | None:
    if name not in _DOXIE_STRUCTURED_RESULT_TOOLS:
        return None
    try:
        data = json.loads(result)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    event_name = data.get("doxie_event")
    if name == "test_agent_profile":
        if event_name != "agent_profile_test_completed":
            return None
        return data
    if event_name not in {
        "agent_profile_draft_requested",
        "agent_profile_design_draft_requested",
        "agent_profile_design_draft_saved",
        "agent_profile_draft_saved",
    }:
        return None
    draft = data.get("draft")
    if not isinstance(draft, dict):
        return None
    event = {
        "doxie_event": event_name,
        "draft": draft,
    }
    operation = data.get("operation")
    if operation in {"create", "update", "upsert", "revision"}:
        event["operation"] = operation
    return event


def _session_interrupted(session: dict | None) -> bool:
    if not session:
        return False
    interrupted_run_id = str(session.get("interrupted_run_id") or "")
    interrupted_turn_id = str(session.get("interrupted_turn_id") or "")
    active_run_id = str(session.get("active_run_id") or "")
    active_turn_id = str(session.get("active_turn_id") or "")
    return bool(
        (interrupted_run_id and (not active_run_id or interrupted_run_id == active_run_id))
        or (interrupted_turn_id and (not active_turn_id or interrupted_turn_id == active_turn_id))
    )


def _on_tool_start(sid: str, tool_call_id: str, name: str, args: dict):
    session = _sessions.get(sid)
    if _session_interrupted(session):
        return
    enabled = _tool_progress_enabled(sid)
    if session is not None:
        try:
            from agent.display import capture_local_edit_snapshot

            snapshot = capture_local_edit_snapshot(name, args)
            if snapshot is not None:
                session.setdefault("edit_snapshots", {})[tool_call_id] = snapshot
        except Exception:
            pass
        session.setdefault("tool_started_at", {})[tool_call_id] = time.time()
    if enabled:
        # tool.complete is the source of truth for todos (full list from the
        # tool result). args.todos here may be a partial merge update.
        _emit(
            "tool.start",
            sid,
            {
                "tool_id": tool_call_id,
                "name": name,
                "context": _tool_ctx(name, args),
                "arguments": _tool_args_payload(args),
            },
        )


def _on_tool_complete(sid: str, tool_call_id: str, name: str, args: dict, result: str):
    session = _sessions.get(sid)
    if _session_interrupted(session):
        return
    payload = {
        "tool_id": tool_call_id,
        "name": name,
        "context": _tool_ctx(name, args),
        "arguments": _tool_args_payload(args),
    }
    snapshot = None
    started_at = None
    if session is not None:
        snapshot = session.setdefault("edit_snapshots", {}).pop(tool_call_id, None)
        started_at = session.setdefault("tool_started_at", {}).pop(tool_call_id, None)
    duration_s = time.time() - started_at if started_at else None
    if duration_s is not None:
        payload["duration_s"] = duration_s
    summary = _tool_summary(name, result, duration_s)
    if summary:
        payload["summary"] = summary
    doxie_result = _doxie_structured_tool_result(name, result)
    if doxie_result:
        payload["result"] = doxie_result
    if name == "todo":
        try:
            data = json.loads(result)
            if isinstance(data, dict) and isinstance(data.get("todos"), list):
                payload["todos"] = data.get("todos")
        except Exception:
            pass
    try:
        from agent.display import render_edit_diff_with_delta

        rendered: list[str] = []
        if render_edit_diff_with_delta(
            name,
            result,
            function_args=args,
            snapshot=snapshot,
            print_fn=rendered.append,
        ):
            payload["inline_diff"] = "\n".join(rendered)
    except Exception:
        pass
    enabled = _tool_progress_enabled(sid)
    if enabled or payload.get("inline_diff") or doxie_result:
        _emit("tool.complete", sid, payload)
    _emit_artifacts_from_tool_complete(sid, tool_call_id, name, args, result)


def _emit_artifacts_from_tool_complete(
    sid: str,
    tool_call_id: str,
    name: str,
    args: dict,
    result: str,
) -> None:
    session = _sessions.get(sid)
    if not session:
        return
    if _session_interrupted(session):
        return
    if session.get("transient"):
        return
    cwd = _session_cwd(session)
    workspace = dict(session.get("workspace") or {})
    session_key = session.get("session_key") or sid
    origin = {
        key: value
        for key, value in {
            "run_id": str(session.get("active_run_id") or ""),
            "turn_id": str(session.get("active_turn_id") or ""),
            "client_message_id": str((session.get("pending_turn") or {}).get("client_message_id") or ""),
        }.items()
        if value
    }
    for payload in record_artifacts_from_tool_complete(
        session_id=session_key,
        tool_call_id=tool_call_id,
        name=name,
        args=args,
        result=result,
        cwd=cwd,
        workspace=workspace,
        origin=origin,
    ):
        _emit(
            "artifact.created",
            sid,
            payload,
        )


def _on_tool_progress(
    sid: str,
    event_type: str,
    name: str | None = None,
    preview: str | None = None,
    _args: dict | None = None,
    **_kwargs,
):
    if _session_interrupted(_sessions.get(sid)):
        return
    enabled = _tool_progress_enabled(sid)
    if not enabled:
        return
    if event_type == "tool.started" and name:
        # The Doxie desktop gateway also receives the structured
        # tool_start_callback for the same tool call, including the stable
        # provider tool id. Emitting this legacy progress-only event would
        # create a duplicate row that can never be reconciled with
        # tool.complete because it has no tool id.
        return
    if event_type == "reasoning.available" and preview:
        _emit("reasoning.available", sid, {"text": str(preview)})
        return
    if event_type.startswith("subagent."):
        payload = {
            "goal": str(_kwargs.get("goal") or ""),
            "task_count": int(_kwargs.get("task_count") or 1),
            "task_index": int(_kwargs.get("task_index") or 0),
        }
        # Identity fields for the TUI spawn tree.  All optional — older
        # emitters that omit them fall back to flat rendering client-side.
        if _kwargs.get("subagent_id"):
            payload["subagent_id"] = str(_kwargs["subagent_id"])
        if _kwargs.get("parent_id"):
            payload["parent_id"] = str(_kwargs["parent_id"])
        if _kwargs.get("depth") is not None:
            payload["depth"] = int(_kwargs["depth"])
        if _kwargs.get("model"):
            payload["model"] = str(_kwargs["model"])
        if _kwargs.get("tool_count") is not None:
            payload["tool_count"] = int(_kwargs["tool_count"])
        if _kwargs.get("toolsets"):
            payload["toolsets"] = [str(t) for t in _kwargs["toolsets"]]
        # Per-branch rollups emitted on subagent.complete (features 1+2+4).
        for int_key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "api_calls",
        ):
            val = _kwargs.get(int_key)
            if val is not None:
                try:
                    payload[int_key] = int(val)
                except (TypeError, ValueError):
                    pass
        if _kwargs.get("cost_usd") is not None:
            try:
                payload["cost_usd"] = float(_kwargs["cost_usd"])
            except (TypeError, ValueError):
                pass
        if _kwargs.get("files_read"):
            payload["files_read"] = [str(p) for p in _kwargs["files_read"]]
        if _kwargs.get("files_written"):
            payload["files_written"] = [str(p) for p in _kwargs["files_written"]]
        if _kwargs.get("output_tail"):
            payload["output_tail"] = list(_kwargs["output_tail"])  # list of dicts
        if name:
            payload["tool_name"] = str(name)
        if preview:
            payload["text"] = str(preview)
        if _kwargs.get("status"):
            payload["status"] = str(_kwargs["status"])
        if _kwargs.get("summary"):
            payload["summary"] = str(_kwargs["summary"])
        if _kwargs.get("duration_seconds") is not None:
            payload["duration_seconds"] = float(_kwargs["duration_seconds"])
        if preview and event_type == "subagent.tool":
            payload["tool_preview"] = str(preview)
            payload["text"] = str(preview)
        _emit(event_type, sid, payload)


def _agent_cbs(sid: str) -> dict:
    return {
        "tool_start_callback": lambda tc_id, name, args: _on_tool_start(
            sid, tc_id, name, args
        ),
        "tool_complete_callback": lambda tc_id, name, args, result: _on_tool_complete(
            sid, tc_id, name, args, result
        ),
        "tool_progress_callback": lambda event_type, name=None, preview=None, args=None, **kwargs: _on_tool_progress(
            sid, event_type, name, preview, args, **kwargs
        ),
        "tool_gen_callback": lambda name: _tool_progress_enabled(sid)
        and _emit("tool.generating", sid, {"name": name}),
        # AIAgent thinking_callback is Hermes' local waiting/musing status,
        # not provider/model reasoning. Keep it separate so clients do not
        # create fake reasoning panes for models that emitted no reasoning.
        "thinking_callback": lambda text: _emit("agent.musing", sid, {"text": text}),
        "reasoning_callback": lambda text: _emit(
            "reasoning.delta", sid, {"text": text, "source": "provider_reasoning"}
        ),
        "status_callback": lambda kind, text=None: _status_update(
            sid, str(kind), None if text is None else str(text)
        ),
        "clarify_callback": lambda q, c: _block(
            "clarify.request", sid, {"question": q, "choices": c}
        ),
    }


def _wire_callbacks(sid: str):
    from tools.terminal_tool import set_sudo_password_callback
    from tools.skills_tool import set_secret_capture_callback

    set_sudo_password_callback(lambda: _block("sudo.request", sid, {}, timeout=120))

    def secret_cb(env_var, prompt, metadata=None):
        pl = {"prompt": prompt, "env_var": env_var}
        if metadata:
            pl["metadata"] = metadata
        val = _block("secret.request", sid, pl)
        if not val:
            return {
                "success": True,
                "stored_as": env_var,
                "validated": False,
                "skipped": True,
                "message": "skipped",
            }
        from hermes_cli.config import save_env_value_secure

        return {
            **save_env_value_secure(env_var, val),
            "skipped": False,
            "message": "ok",
        }

    set_secret_capture_callback(secret_cb)


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
        or _session_enabled_toolsets(_sessions.get(task_id)),
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
    profile_tokens = _enter_profile_context(session.get("profile_context"))
    tokens = _set_session_context(session["session_key"], terminal_cwd=session.get("cwd"))
    try:
        new_agent = _make_agent(
            sid,
            session["session_key"],
            session_id=session["session_key"],
            cwd=session.get("cwd"),
        )
    finally:
        _clear_session_context(tokens)
        _leave_profile_context(profile_tokens)
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
    info = _session_info(new_agent, session)
    _emit("session.info", sid, info)
    _restart_slash_worker(session)
    return info


def _make_agent(
    sid: str,
    key: str,
    session_id: str | None = None,
    cwd: str | None = None,
):
    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider

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
        enabled_toolsets=_session_enabled_toolsets(_sessions.get(sid)),
        platform="tui",
        session_id=session_id or key,
        session_db=None if (_sessions.get(sid) or {}).get("transient") else _get_db(),
        ephemeral_system_prompt=system_prompt or None,
        checkpoints_enabled=is_truthy_value(os.environ.get("HERMES_TUI_CHECKPOINTS")),
        pass_session_id=is_truthy_value(os.environ.get("HERMES_TUI_PASS_SESSION_ID")),
        skip_context_files=is_truthy_value(os.environ.get("HERMES_IGNORE_RULES")),
        skip_memory=is_truthy_value(os.environ.get("HERMES_IGNORE_RULES")),
        **_agent_cbs(sid),
    )
    setattr(
        agent,
        "_gateway_runtime_requested_provider",
        runtime.get("requested_provider") or requested_provider or "",
    )
    agent.session_cwd = _normalize_session_cwd(cwd)
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
):
    _sessions[sid] = {
        "agent": agent,
        "session_key": key,
        "cwd": _normalize_session_cwd(cwd),
        "profile_context": profile_context,
        "workspace": dict(workspace or {}),
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "active_run_id": None,
        "active_turn_id": None,
        "pending_turn": None,
        "model_descriptor": dict(getattr(agent, "model_descriptor", {}) or {}),
        "run_started_at": 0,
        "run_updated_at": 0,
        "event_seq": 0,
        "interrupted_run_id": "",
        "interrupted_turn_id": "",
        "interrupt_seq": 0,
        "recalled_turn_ids": set(),
        "attached_images": [],
        "image_counter": 0,
        "cols": cols,
        "slash_worker": None,
        "show_reasoning": _load_show_reasoning(),
        "tool_progress_mode": _requested_tool_progress_mode(),
        "edit_snapshots": {},
        "tool_started_at": {},
        # Pin async event emissions to whichever transport created the
        # session (stdio for Ink, JSON-RPC WS for the dashboard sidebar).
        "transport": current_transport() or _stdio_transport,
    }
    try:
        _sessions[sid]["slash_worker"] = _SlashWorker(
            key, getattr(agent, "model", _resolve_model())
        )
    except Exception:
        # Defer hard-failure to slash.exec; chat still works without slash worker.
        _sessions[sid]["slash_worker"] = None
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
    _notify_session_boundary("on_session_reset", key)
    _emit("session.info", sid, _session_info(agent, _sessions[sid]))


def _new_session_key() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _with_checkpoints(session, fn):
    return fn(session["agent"]._checkpoint_mgr, _session_cwd(session))


def _resolve_checkpoint_hash(mgr, cwd: str, ref: str) -> str:
    try:
        checkpoints = mgr.list_checkpoints(cwd)
        idx = int(ref) - 1
    except ValueError:
        return ref
    if 0 <= idx < len(checkpoints):
        return checkpoints[idx].get("hash", ref)
    raise ValueError(f"Invalid checkpoint number. Use 1-{len(checkpoints)}.")


def _enrich_with_attached_images(user_text: str, image_paths: list[str]) -> str:
    """Pre-analyze attached images via vision and prepend descriptions to user text."""
    import asyncio, json as _json
    from tools.vision_tools import vision_analyze_tool

    prompt = (
        "Describe everything visible in this image in thorough detail. "
        "Include any text, code, data, objects, people, layout, colors, "
        "and any other notable visual information."
    )

    parts: list[str] = []
    for path in image_paths:
        p = Path(path)
        if not p.exists():
            continue
        hint = f"[You can examine it with vision_analyze using image_url: {p}]"
        try:
            r = _json.loads(
                asyncio.run(vision_analyze_tool(image_url=str(p), user_prompt=prompt))
            )
            desc = r.get("analysis", "") if r.get("success") else None
            parts.append(
                f"[The user attached an image:\n{desc}]\n{hint}"
                if desc
                else f"[The user attached an image but analysis failed.]\n{hint}"
            )
        except Exception:
            parts.append(f"[The user attached an image but analysis failed.]\n{hint}")

    text = user_text or ""
    prefix = "\n\n".join(parts)
    if prefix:
        return f"{prefix}\n\n{text}" if text else prefix
    return text or "What do you see in this image?"


def _content_display_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        parts = []
        for part in content:
            text = _content_display_text(part).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in {"text", "input_text", "output_text"}:
            return str(content.get("text") or content.get("content") or "")
        if kind in {"image_url", "input_image", "image"}:
            return "[image]"
        if kind in {"input_audio", "audio"}:
            return "[audio]"
        if kind:
            return f"[{kind}]"
        if "text" in content:
            return str(content.get("text") or "")
        return "[structured content]"
    return str(content)


def _history_to_messages(history: list[dict]) -> list[dict]:
    messages = []
    tool_call_args = {}

    for m in history:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in {"user", "assistant", "tool", "system"}:
            continue
        content_text = _content_display_text(m.get("content"))
        reasoning_text = ""
        if role == "assistant":
            reasoning_text = _content_display_text(
                m.get("reasoning") or m.get("reasoning_content")
            )
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                tc_id = tc.get("id", "")
                if tc_id and fn.get("name"):
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_call_args[tc_id] = (fn["name"], args)
            if not content_text.strip() and not reasoning_text.strip():
                continue
        if role == "tool":
            tc_id = m.get("tool_call_id", "")
            tc_info = tool_call_args.get(tc_id) if tc_id else None
            name = (tc_info[0] if tc_info else None) or m.get("tool_name") or "tool"
            args = (tc_info[1] if tc_info else None) or {}
            item = {
                "role": "tool",
                "name": name,
                "context": _tool_ctx(name, args),
            }
            if args:
                item["arguments"] = _tool_args_payload(args)
            messages.append(
                item
            )
            continue
        if not content_text.strip() and not reasoning_text.strip():
            continue
        message = {"role": role, "text": content_text}
        if isinstance(m.get("metadata"), dict):
            message["metadata"] = dict(m["metadata"])
        if reasoning_text.strip():
            message["reasoning"] = reasoning_text
        messages.append(message)

    return messages

# ── Method registration ───────────────────────────────────────────────

from tui_gateway.core.method_registration import register_method_modules

register_method_modules(globals())
