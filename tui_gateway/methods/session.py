# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import base64
import json
import queue

from doxie_extension.display_transcript import (
    sanitize_session_list_item,
    sanitize_transcript_messages,
)
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services import run_control
from tui_gateway.services.workspace import (
    bind_session_workspace as _bind_session_workspace,
    normalize_session_cwd as _normalize_session_cwd,
    workspace_for_session as _workspace_for_session,
    workspace_from_params as _workspace_from_params,
)

_server = bind_server_globals(globals())
_interrupt_work_queue: queue.SimpleQueue = queue.SimpleQueue()
_agent_interrupt_work_queue: queue.SimpleQueue = queue.SimpleQueue()
_INTERNAL_SESSION_LIST_SOURCES = ("tool", "cron")


def _interrupt_trace(message: str) -> None:
    if not is_truthy_value(os.environ.get("HERMES_INTERRUPT_TRACE")):
        return
    print(message, file=sys.stderr, flush=True)


def _interrupt_work_loop() -> None:
    while True:
        fn = _interrupt_work_queue.get()
        try:
            fn()
        except Exception as exc:
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] interrupt.work.failed error={type(exc).__name__}: {exc}",
            )


threading.Thread(
    target=_interrupt_work_loop,
    name="tui-session-interrupt-worker",
    daemon=True,
).start()


def _agent_interrupt_work_loop() -> None:
    while True:
        fn = _agent_interrupt_work_queue.get()
        try:
            fn()
        except Exception as exc:
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.work.failed error={type(exc).__name__}: {exc}",
            )


threading.Thread(
    target=_agent_interrupt_work_loop,
    name="tui-agent-interrupt-worker",
    daemon=True,
).start()


def _schedule_interrupt_work(fn) -> None:
    _interrupt_work_queue.put(fn)


def _schedule_agent_interrupt_work(fn) -> None:
    _agent_interrupt_work_queue.put(fn)


# ── Methods: session ─────────────────────────────────────────────────


def _stored_workspace(session_id: str) -> dict | None:
    try:
        return _workspace_for_session(session_id)
    except Exception:
        return None


def _requested_runtime_scope_key(params: dict | None = None) -> str:
    return str(
        (params or {}).get("runtime_scope_key")
        or (params or {}).get("runtimeScopeKey")
        or ""
    ).strip()


def _requested_tool_progress_mode(params: dict | None = None) -> str:
    raw = (
        (params or {}).get("tool_progress_mode")
        or (params or {}).get("toolProgressMode")
        or (params or {}).get("tool_progress")
        or (params or {}).get("toolProgress")
    )
    if raw is False:
        return "off"
    if raw is True:
        return "all"
    mode = str(raw or "").strip().lower()
    if mode in {"off", "new", "all", "verbose"}:
        return mode
    return _load_tool_progress_mode()


def _session_run_snapshot(runtime_sid: str, session: dict | None, db=None) -> dict:
    session = session or {}
    stable_session_id = str(session.get("session_key") or runtime_sid or "")
    control_state = run_control.session_status(stable_session_id, db=db)
    running = bool(session.get("running") or control_state.get("running"))
    return {
        "running": running,
        "runtime_scope_key": str(control_state.get("runtime_scope_key") or session.get("active_runtime_scope_key") or stable_session_id),
        "active_runtime_session_id": runtime_sid or "",
        "active_run_id": str(session.get("active_run_id") or control_state.get("active_run_id") or "") if running else "",
        "active_turn_id": str(session.get("active_turn_id") or control_state.get("active_turn_id") or "") if running else "",
        "run_started_at": (session.get("run_started_at") or control_state.get("run_started_at") or 0) if running else 0,
        "run_updated_at": (session.get("run_updated_at") or control_state.get("run_updated_at") or 0) if running else 0,
        "last_event_seq": int(control_state.get("last_event_seq") or 0),
    }


def _live_sessions_by_stored_key() -> dict[str, tuple[str, dict]]:
    try:
        snapshot = list(_sessions.items())
    except RuntimeError:
        snapshot = list(_sessions.items())
    live: dict[str, tuple[str, dict]] = {}
    for sid, session in snapshot:
        key = str((session or {}).get("session_key") or "")
        if not key:
            continue
        current = live.get(key)
        if current is None:
            live[key] = (sid, session)
            continue
        _current_sid, current_session = current
        current_rank = (
            1 if current_session.get("running") else 0,
            float(current_session.get("run_updated_at") or 0),
            float(current_session.get("run_started_at") or 0),
        )
        candidate_rank = (
            1 if session.get("running") else 0,
            float(session.get("run_updated_at") or 0),
            float(session.get("run_started_at") or 0),
        )
        if candidate_rank > current_rank:
            live[key] = (sid, session)
    return live


def _is_empty_stored_conversation(row: dict) -> bool:
    """Return true only when a rich session row is explicitly content-empty.

    ``session.list`` can be backed by older tests or alternate DB shims that
    expose a minimal session row. Treating missing ``message_count`` /
    ``title`` / ``preview`` fields as empty makes the gateway silently hide
    real history rows. Only the canonical rich-row shape can prove that a
    stored row is an empty placeholder.
    """
    if not all(key in row for key in ("message_count", "title", "preview")):
        return False
    return (
        int(row.get("message_count") or 0) <= 0
        and not (row.get("title") or "").strip()
        and not (row.get("preview") or "").strip()
    )


def _request_agent_interrupt_async(sid: str, agent) -> None:
    if agent is None or not hasattr(agent, "interrupt"):
        _interrupt_trace(
            f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.skip sid={sid} has_agent={agent is not None}",
        )
        return

    def run_interrupt() -> None:
        started_at = time.time()
        _interrupt_trace(
            f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.begin sid={sid}",
        )
        try:
            agent.interrupt()
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.done sid={sid} elapsed_ms={int((time.time() - started_at) * 1000)}",
            )
        except Exception as exc:
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.failed sid={sid} error={type(exc).__name__}: {exc}",
            )

    _schedule_agent_interrupt_work(run_interrupt)


def _request_session_interrupt_side_effects_async(
    *,
    sid: str,
    session: dict,
    should_interrupt_agent: bool,
    interrupted_run_id: str,
    interrupted_turn_id: str,
    completion_status: str = "interrupted",
) -> None:
    def run_side_effects() -> None:
        if should_interrupt_agent:
            with session["history_lock"]:
                active_run_id = str(session.get("active_run_id") or "")
                active_turn_id = str(session.get("active_turn_id") or "")
                if (
                    (active_run_id and active_run_id != interrupted_run_id)
                    or (active_turn_id and active_turn_id != interrupted_turn_id)
                ):
                    _interrupt_trace(
                        "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.side_effects.skip_stale "
                        f"sid={sid} interrupted_run_id={interrupted_run_id or '-'} "
                        f"interrupted_turn_id={interrupted_turn_id or '-'} active_run_id={active_run_id or '-'} "
                        f"active_turn_id={active_turn_id or '-'}",
                    )
                    return
            _interrupt_trace(
                "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.side_effects.begin "
                f"sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'}",
            )
            _request_agent_interrupt_async(sid, session.get("agent"))
            # Scope pending prompt release to THIS session. A global
            # _clear_pending() would collaterally cancel clarify/sudo/secret
            # prompts on unrelated sessions sharing the same gateway process.
            _clear_pending(sid)
            try:
                from tools.approval import resolve_gateway_approval

                resolve_gateway_approval(session["session_key"], "deny", resolve_all=True)
            except Exception:
                pass
            _emit(
                "message.complete",
                sid,
                {
                    "text": "",
                    "status": completion_status,
                    "run_id": interrupted_run_id,
                    "turn_id": interrupted_turn_id,
                },
            )
            _interrupt_trace(
                "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.side_effects.done "
                f"sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'}",
            )

    _schedule_interrupt_work(run_side_effects)


def _bounded_page_limit(value, *, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _encode_page_cursor(payload: dict | None) -> str:
    if not payload:
        return ""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_page_cursor(value) -> dict:
    token = str(value or "").strip()
    if not token:
        return {}
    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _message_page_info(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    prev_id = raw.get("prev_cursor_id")
    next_id = raw.get("next_cursor_id")
    return {
        "prevCursor": _encode_page_cursor({"id": prev_id}) if prev_id is not None else "",
        "nextCursor": _encode_page_cursor({"id": next_id}) if next_id is not None else "",
        "hasMoreBefore": bool(raw.get("hasMoreBefore")),
        "hasMoreAfter": bool(raw.get("hasMoreAfter")),
        "totalCount": int(raw.get("totalCount") or 0),
    }


def _display_history_page(db, session_id: str, hydrate: str, limit: int) -> tuple[list[dict], dict]:
    mode = hydrate if hydrate in {"full", "tail", "none"} else "full"
    if mode == "none":
        return [], {
            "prevCursor": "",
            "nextCursor": "",
            "hasMoreBefore": False,
            "hasMoreAfter": False,
            "totalCount": 0,
        }
    if mode == "tail":
        page = db.get_messages_page_as_conversation(
            session_id,
            direction="tail",
            limit=limit,
            include_ancestors=True,
        )
        return (
            sanitize_transcript_messages(_history_to_messages(page.get("messages") or [])),
            _message_page_info(page.get("pageInfo")),
        )
    try:
        display_history = db.get_messages_as_conversation(
            session_id,
            include_ancestors=True,
            include_storage_metadata=True,
        )
    except TypeError:
        display_history = db.get_messages_as_conversation(
            session_id,
            include_ancestors=True,
        )
    messages = sanitize_transcript_messages(_history_to_messages(display_history))
    page_info = {
        "prevCursor": "",
        "nextCursor": "",
        "hasMoreBefore": False,
        "hasMoreAfter": False,
        "totalCount": len(messages),
    }
    return messages, page_info


@method("session.create")
def _(rid, params: dict) -> dict:
    sid = uuid.uuid4().hex[:8]
    key = _new_session_key()
    cols = int(params.get("cols", 80))
    transient = bool(params.get("transient") or params.get("temporary") or params.get("ephemeral"))
    control_plane_only = bool(
        params.get("control_plane_only")
        or params.get("controlPlaneOnly")
        or params.get("defer_agent_build")
        or params.get("deferAgentBuild")
    )
    tool_progress_mode = _requested_tool_progress_mode(params)
    runtime_scope_key = _requested_runtime_scope_key(params)
    try:
        cwd = _normalize_session_cwd(params.get("cwd"))
        workspace = _workspace_from_params(params, cwd)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    try:
        workspace = _bind_session_workspace(
            session_id=key,
            cwd=cwd,
            workspace=workspace,
        )
    except Exception as exc:
        return _err(rid, 5012, f"workspace bind failed: {exc}")
    db = _get_db()
    if db is None and control_plane_only:
        return _db_unavailable_error(rid, code=5000)
    model = _resolve_model()
    if db is not None:
        try:
            db.create_session(key, source="tui", model=model, transient=transient)
        except Exception as exc:
            return _err(rid, 5000, f"session create failed: {exc}")

    if control_plane_only:
        return _ok(
            rid,
            {
                "session_id": key,
                "stored_session_id": key,
                "info": {
                    "model": model,
                    "tools": {},
                    "skills": {},
                    "cwd": cwd,
                    "workspace": workspace,
                    "lazy": True,
                    "transient": transient,
                    "control_plane_only": True,
                },
            },
        )

    _enable_gateway_prompts()
    ready = threading.Event()

    _sessions[sid] = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
        "attached_images": [],
        "cols": cols,
        "cwd": cwd,
        "edit_snapshots": {},
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "image_counter": 0,
        "pending_title": None,
        "profile_context": _profile_context_for_params(params),
        "runtime_scope_key": runtime_scope_key,
        "running": False,
        "active_run_id": None,
        "active_turn_id": None,
        "pending_turn": None,
        "run_started_at": 0,
        "run_updated_at": 0,
        "event_seq": 0,
        "interrupted_run_id": "",
        "interrupted_turn_id": "",
        "interrupt_seq": 0,
        "recalled_turn_ids": set(),
        "session_key": key,
        "show_reasoning": _load_show_reasoning(),
        "slash_worker": None,
        "tool_progress_mode": tool_progress_mode,
        "tool_started_at": {},
        "transport": current_transport() or _stdio_transport,
        "transient": transient,
        "workspace": workspace,
    }

    if not control_plane_only:
        # Legacy TUI compatibility: return the lightweight session first, then
        # build the AIAgent shortly after response flush. Doxie/new run.*
        # callers should pass control_plane_only/defer_agent_build and let
        # run.submit lazily attach the runtime.
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
            "info": {
                "model": _resolve_model(),
                "tools": {},
                "skills": {},
                "cwd": cwd,
                "workspace": workspace,
                "lazy": True,
                "transient": transient,
                "control_plane_only": control_plane_only,
            },
        },
    )


@method("session.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(
            rid,
            {
                "sessions": [],
                "pageInfo": {
                    "nextCursor": "",
                    "hasMore": False,
                },
            },
        )
    try:
        # Resume picker should surface human conversation sessions from every
        # user-facing surface — CLI, TUI, all gateway platforms (including new
        # ones not enumerated here), ACP adapter clients, webhook sessions,
        # custom `HERMES_SESSION_SOURCE` values, and older installs with
        # different source labels. We deny-list only noisy internal runtime
        # sources rather than allow-listing a fixed set of platform names
        # that goes stale whenever a new platform is added or a user names
        # their own source.
        #
        # ``tool`` rows are sub-agent runs. ``cron`` rows are scheduler
        # execution contexts; Doxie current-session/new-session result
        # bindings project their user-visible output into the target
        # conversation, so surfacing the raw cron session creates duplicate
        # sidebar conversations that begin with the internal cron prompt.
        deny = _INTERNAL_SESSION_LIST_SOURCES

        limit = _bounded_page_limit(params.get("limit"), default=200, maximum=200)
        cursor = _decode_page_cursor(params.get("cursor"))
        rows = [
            s
            for s in db.list_sessions_rich(
                source=None,
                exclude_sources=list(deny),
                limit=limit + 1,
                page_cursor=cursor,
                order_by_last_active=True,
            )
            if (s.get("source") or "").strip().lower() not in deny
        ]
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        live_by_key = _live_sessions_by_stored_key()
        session_items = []
        for s in page_rows:
            live_sid, live_session = live_by_key.get(s["id"], ("", None))
            live_state = _session_run_snapshot(live_sid, live_session, db=db)
            if not live_sid and _is_empty_stored_conversation(s):
                continue
            session_items.append(
                sanitize_session_list_item({
                    "id": s["id"],
                    "title": s.get("title") or "",
                    "preview": s.get("preview") or "",
                    "started_at": s.get("started_at") or 0,
                    "updated_at": s.get("last_active") or s.get("started_at") or 0,
                    "message_count": s.get("message_count") or 0,
                    "source": s.get("source") or "",
                    "workspace": _stored_workspace(s["id"]),
                    **live_state,
                })
            )
        next_cursor = ""
        if has_more and page_rows:
            cursor_payload = page_rows[-1].get("_page_cursor") or {
                "effective_last_active": page_rows[-1].get("last_active") or page_rows[-1].get("started_at") or 0,
                "started_at": page_rows[-1].get("started_at") or 0,
                "id": page_rows[-1].get("id") or "",
            }
            next_cursor = _encode_page_cursor(cursor_payload)
        return _ok(
            rid,
            {
                "sessions": session_items,
                "pageInfo": {
                    "nextCursor": next_cursor,
                    "hasMore": has_more,
                },
            },
        )
    except Exception as e:
        return _err(rid, 5006, str(e))


@method("session.most_recent")
def _(rid, params: dict) -> dict:
    """Return the most recent human-facing session id, or ``None``.

    Mirrors ``session.list``'s deny-list behaviour (drops internal
    runtime rows such as sub-agent and cron execution sessions). Used by
    TUI auto-resume when
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
        deny = frozenset(_INTERNAL_SESSION_LIST_SOURCES)
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
    db = _get_db()
    if db is None:
        return _err(rid, 4007, "session not found")
    found = db.get_session(target)
    if not found:
        found = db.get_session_by_title(target)
        if found:
            target = found["id"]
        else:
            return _err(rid, 4007, "session not found")
    existing_workspace = _stored_workspace(target)
    raw_cwd = params.get("cwd") or (existing_workspace or {}).get("cwd")
    workspace_params = params
    if not params.get("workspace") and existing_workspace:
        workspace_params = {**params, "workspace": existing_workspace}
    try:
        cwd = _normalize_session_cwd(raw_cwd)
        workspace = _workspace_from_params(workspace_params, cwd)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    try:
        workspace = _bind_session_workspace(
            session_id=target,
            cwd=cwd,
            workspace=workspace,
        )
    except Exception as exc:
        return _err(rid, 5012, f"workspace bind failed: {exc}")
    sid = uuid.uuid4().hex[:8]
    _enable_gateway_prompts()
    hydrate = str(params.get("hydrate") or "full").strip().lower()
    runtime_scope_key = _requested_runtime_scope_key(params)
    message_limit = _bounded_page_limit(
        params.get("message_limit", params.get("messageLimit")),
        default=50,
        maximum=200,
    )
    try:
        db.reopen_session(target)
        history = db.get_messages_as_conversation(target)
        messages, message_page_info = _display_history_page(db, target, hydrate, message_limit)
        live_sid, live_session = _resolve_runtime_session(target)
        if live_session is not None:
            live_runtime_scope_key = str(
                live_session.get("runtime_scope_key")
                or live_session.get("active_runtime_scope_key")
                or ""
            ).strip()
            if (
                params.get("_runtime_attach")
                and runtime_scope_key
                and live_runtime_scope_key
                and live_runtime_scope_key != runtime_scope_key
            ):
                live_session = None
            else:
                live_session["transport"] = (
                    current_transport()
                    or live_session.get("transport")
                    or _stdio_transport
                )
                live_session["cwd"] = cwd
                live_session["workspace"] = workspace
                if runtime_scope_key:
                    live_session["runtime_scope_key"] = runtime_scope_key
                live_state = _session_run_snapshot(live_sid, live_session, db=db)
                return _ok(
                    rid,
                    {
                        "session_id": live_sid,
                        "resumed": target,
                        "message_count": message_page_info.get("totalCount") or len(messages),
                        "messages": messages,
                        "messagePageInfo": message_page_info,
                        "info": _session_info(live_session.get("agent"), live_session),
                        **live_state,
                    },
                )
        profile_context = _profile_context_for_params(params)
        profile_tokens = _enter_profile_context(profile_context)
        tokens = _set_session_context(target, terminal_cwd=cwd)
        try:
            try:
                agent = _make_agent(sid, target, session_id=target, cwd=cwd)
            except TypeError as exc:
                if "unexpected keyword argument 'cwd'" not in str(exc):
                    raise
                agent = _make_agent(sid, target, session_id=target)
        finally:
            _clear_session_context(tokens)
            _leave_profile_context(profile_tokens)
        try:
            _init_session(
                sid,
                target,
                agent,
                history,
                cols=int(params.get("cols", 80)),
                cwd=cwd,
                workspace=workspace,
                profile_context=profile_context,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            _init_session(sid, target, agent, history, cols=int(params.get("cols", 80)))
        if sid in _sessions:
            _sessions[sid]["runtime_scope_key"] = runtime_scope_key
    except Exception as e:
        return _err(rid, 5000, f"resume failed: {e}")
    return _ok(
        rid,
        {
            "session_id": sid,
            "resumed": target,
            "message_count": message_page_info.get("totalCount") or len(messages),
            "messages": messages,
            "messagePageInfo": message_page_info,
            "info": (
                _session_info(agent, _sessions.get(sid))
                if _sessions.get(sid) is not None
                else _session_info(agent)
            ),
            **_session_run_snapshot(sid, _sessions.get(sid), db=db),
        },
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
    run_state = run_control.session_status(target, db=db)
    if run_state.get("running"):
        return _err(rid, 4023, "cannot delete a session with an active run")
    # Block deletion of any session currently bound to a live TUI session
    # in this process.  The picker hides the active session anyway, but a
    # racing caller could still target it.  Snapshot via ``list(...)``
    # because ``_sessions`` is mutated by concurrent RPCs on the thread
    # pool — iterating the dict directly can raise ``RuntimeError:
    # dictionary changed size during iteration``.  If even the snapshot
    # raises, fail closed (refuse the delete) rather than fail open.
    try:
        snapshot = list(_sessions.values())
    except Exception as e:
        return _err(rid, 5036, f"could not enumerate active sessions: {e}")
    active = {s.get("session_key") for s in snapshot if s.get("session_key")}
    if target in active:
        return _err(rid, 4023, "cannot delete an active session")
    sessions_dir = Path(get_hermes_home()) / "sessions"
    try:
        deleted = db.delete_session(target, sessions_dir=sessions_dir)
    except Exception as e:
        return _err(rid, 5036, f"delete failed: {e}")
    if not deleted:
        return _err(rid, 4007, "session not found")
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
        # Queue only when the session row truly does not exist yet.
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
        session["pending_title"] = title
        return _ok(rid, {"pending": True, "title": title})
    except ValueError as e:
        return _err(rid, 4022, str(e))
    except Exception as e:
        return _err(rid, 5007, str(e))


@method("session.usage")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    agent = session.get("agent")
    key = str(session.get("session_key") or params.get("session_id") or "")
    updated_at = int(time.time() * 1000)
    if agent is None:
        usage = {"calls": 0, "input": 0, "output": 0, "total": 0}
        provider = "unknown"
        model = "unknown"
    else:
        usage = _get_usage(agent)
        provider = getattr(agent, "provider", None) or "unknown"
        model = str(usage.get("model") or getattr(agent, "model", None) or "unknown")

    structured = {
        "updatedAt": updated_at,
        "sessions": [
            {
                "key": key,
                "usage": {
                    "modelUsage": [
                        {
                            "provider": provider,
                            "model": model,
                            "count": int(usage.get("calls") or 0),
                            "totals": {
                                "input": int(usage.get("input") or usage.get("prompt") or 0),
                                "output": int(usage.get("output") or usage.get("completion") or 0),
                                "cacheRead": int(usage.get("cache_read") or 0),
                                "cacheWrite": int(usage.get("cache_write") or 0),
                                "reasoning": int(usage.get("reasoning") or 0),
                                "image": int(usage.get("image") or 0),
                                "totalTokens": int(usage.get("total") or 0),
                                "totalCost": float(usage.get("cost_usd") or 0),
                            },
                        }
                    ]
                },
            }
        ],
    }
    structured.update(usage)
    return _ok(
        rid,
        structured,
    )


@method("session.status")
def _(rid, params: dict) -> dict:
    from hermes_constants import display_hermes_home

    requested = str(params.get("session_id") or params.get("stored_session_id") or "").strip()
    if not requested:
        return _err(rid, 4006, "session_id required")
    runtime_sid, session = _resolve_runtime_session(requested)
    key = str((session or {}).get("session_key") or requested)
    agent = (session or {}).get("agent")
    meta = {}
    db = _get_db()
    if db and key:
        try:
            meta = db.get_session(key) or {}
            if not meta:
                by_title = db.get_session_by_title(key)
                if by_title:
                    key = by_title["id"]
                    meta = by_title
        except Exception:
            meta = {}
    if db and not meta and session is None:
        return _err(rid, 4007, "session not found")

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
            f"Agent Running: {'Yes' if (session or {}).get('running') else 'No'}",
        ]
    )
    return _ok(
        rid,
        {
            "output": "\n".join(lines),
            "session_id": runtime_sid,
            "stored_session_id": key,
            **_session_run_snapshot(runtime_sid, session or {"session_key": key}, db=db),
        },
    )


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
            "messages": sanitize_transcript_messages(_history_to_messages(history)),
        },
    )


@method("session.messages")
def _(rid, params: dict) -> dict:
    target = str(params.get("session_id") or "").strip()
    if not target:
        return _err(rid, 4006, "session_id required")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5000)
    found = db.get_session(target)
    if not found:
        found = db.get_session_by_title(target)
        if found:
            target = found["id"]
        else:
            return _err(rid, 4007, "session not found")
    cursor = _decode_page_cursor(params.get("cursor"))
    cursor_id = cursor.get("id")
    try:
        cursor_id = int(cursor_id) if cursor_id is not None else None
    except (TypeError, ValueError):
        cursor_id = None
    try:
        page = db.get_messages_page_as_conversation(
            target,
            direction=str(params.get("direction") or "tail"),
            cursor_id=cursor_id,
            limit=_bounded_page_limit(params.get("limit"), default=50, maximum=200),
            include_ancestors=bool(params.get("include_ancestors", params.get("includeAncestors", True))),
        )
    except Exception as exc:
        return _err(rid, 5000, f"messages page failed: {exc}")
    include_run_events = bool(params.get("include_run_events", params.get("includeRunEvents", False)))
    run_events = []
    if include_run_events:
        try:
            list_run_events = getattr(db, "list_run_events", None)
            if callable(list_run_events):
                run_events = list_run_events(
                    target,
                    runtime_scope_key=_requested_runtime_scope_key(params),
                    limit=_bounded_page_limit(params.get("run_events_limit", params.get("runEventsLimit")), default=2000, maximum=5000),
                )
        except Exception as exc:
            return _err(rid, 5000, f"run event page failed: {exc}")
    return _ok(
        rid,
        {
            "session_id": target,
            "messages": sanitize_transcript_messages(_history_to_messages(page.get("messages") or [])),
            "runEvents": run_events,
            "pageInfo": _message_page_info(page.get("pageInfo")),
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


def _message_turn_id(message: dict) -> str:
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("turn_id") or "")
    return ""


def _draft_from_turn_message(message: dict | None, pending_turn: dict | None = None) -> dict:
    metadata = message.get("metadata") if isinstance(message, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    pending_turn = pending_turn if isinstance(pending_turn, dict) else {}
    text = (
        metadata.get("draft_text")
        or pending_turn.get("draft_text")
        or (message or {}).get("content")
        or pending_turn.get("text")
        or ""
    )
    return {
        "turnId": str(metadata.get("turn_id") or pending_turn.get("turn_id") or ""),
        "text": str(text or ""),
        "attachments": (
            metadata.get("attachments")
            if isinstance(metadata.get("attachments"), list)
            else pending_turn.get("attachments") if isinstance(pending_turn.get("attachments"), list) else []
        ),
        "model": str(metadata.get("model") or pending_turn.get("model") or ""),
    }


def _rewrite_live_and_persisted_history(session: dict, history: list[dict]) -> None:
    session_key = str(session.get("session_key") or "")
    db = _get_db()
    if db is not None and session_key:
        db.replace_messages(session_key, history)
    session["history"] = history
    session["history_version"] = int(session.get("history_version", 0)) + 1
    agent = session.get("agent")
    if agent is not None:
        try:
            agent._session_messages = history
        except Exception:
            pass
        try:
            agent._last_flushed_db_idx = len(history)
        except Exception:
            pass


def _recall_turn_from_history(history: list[dict], turn_id: str, pending_turn: dict | None = None) -> tuple[list[dict], dict, int] | None:
    target_idx = None
    for idx, message in enumerate(history):
        if isinstance(message, dict) and _message_turn_id(message) == turn_id and message.get("role") == "user":
            target_idx = idx
            break
    if target_idx is None:
        return None

    remove_end = len(history)
    for idx in range(target_idx + 1, len(history)):
        message = history[idx]
        if isinstance(message, dict) and message.get("role") == "user":
            remove_end = idx
            break
    target_message = history[target_idx]
    draft = _draft_from_turn_message(target_message, pending_turn)
    next_history = history[:target_idx] + history[remove_end:]
    return next_history, draft, remove_end - target_idx


def _load_stored_history_for_rewrite(db, session_key: str) -> list[dict]:
    try:
        return db.get_messages_as_conversation(
            session_key,
            include_ancestors=False,
            include_storage_metadata=True,
        )
    except TypeError:
        return db.get_messages_as_conversation(session_key, include_ancestors=False)


def _recall_stored_turn(rid, sid: str, turn_id: str) -> dict | None:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5036)
    session_key = sid
    found = db.get_session(session_key)
    if not found:
        found = db.get_session_by_title(session_key)
        if found:
            session_key = found["id"]
        else:
            return None
    try:
        history = _load_stored_history_for_rewrite(db, session_key)
        recalled = _recall_turn_from_history(list(history or []), turn_id)
        if recalled is None:
            return _err(rid, 4019, "turn not found or already recalled")
        next_history, draft, removed = recalled
        db.replace_messages(session_key, next_history)
        messages = sanitize_transcript_messages(_history_to_messages(next_history))
    except Exception as exc:
        return _err(rid, 5036, f"recall failed: {exc}")

    _emit("session.recalled", sid, {
        "turn_id": turn_id,
        "removed_messages": removed,
        "draft": draft,
        "messages": messages,
    })
    return _ok(rid, {
        "status": "recalled",
        "session_id": sid,
        "stored_session_id": session_key,
        "turn_id": turn_id,
        "interrupted": False,
        "removed_messages": removed,
        "draft": draft,
        "messages": messages,
        "memory_retract": {
            "status": "unsupported",
            "warnings": ["Memory provider turn-level retraction is not implemented yet."],
        },
    })


@method("session.recall_turn")
def _(rid, params: dict) -> dict:
    sid = str(params.get("session_id") or "").strip()
    turn_id = str(params.get("turn_id") or "").strip()
    if not turn_id:
        return _err(rid, 4006, "turn_id required")
    runtime_sid, live_session = _resolve_runtime_session(sid)
    if live_session is None:
        stored_result = _recall_stored_turn(rid, sid, turn_id)
        if stored_result is not None:
            return stored_result
    session, err = _sess(params, rid)
    if err:
        return err
    sid = str(params.get("session_id") or runtime_sid or sid)

    interrupted = False
    agent_to_interrupt = None
    with session["history_lock"]:
        active_turn_id = str(session.get("active_turn_id") or "")
        if session.get("running") and active_turn_id and active_turn_id != turn_id:
            return _err(rid, 4009, "session busy with a different turn")
        if session.get("running") and active_turn_id == turn_id:
            interrupted = True
            session["interrupted_run_id"] = str(session.get("active_run_id") or "")
            session["interrupted_turn_id"] = turn_id
            session["interrupt_seq"] = int(session.get("interrupt_seq") or 0) + 1
            session.setdefault("recalled_turn_ids", set()).add(turn_id)
            agent_to_interrupt = session.get("agent")

    if agent_to_interrupt is not None and hasattr(agent_to_interrupt, "interrupt"):
        try:
            agent_to_interrupt.interrupt()
        except Exception:
            pass

    with session["history_lock"]:
        history = list(session.get("history") or [])
        pending_turn = session.get("pending_turn")
        recalled = _recall_turn_from_history(history, turn_id, pending_turn)
        if recalled is None:
            if isinstance(pending_turn, dict) and str(pending_turn.get("turn_id") or "") == turn_id:
                draft = _draft_from_turn_message(None, pending_turn)
                session.setdefault("recalled_turn_ids", set()).add(turn_id)
                session["running"] = False
                session["active_run_id"] = None
                session["active_turn_id"] = None
                session["pending_turn"] = None
                session["run_updated_at"] = time.time()
                messages = sanitize_transcript_messages(_history_to_messages(history))
                _emit("session.recalled", sid, {
                    "turn_id": turn_id,
                    "removed_messages": 0,
                    "draft": draft,
                    "messages": messages,
                })
                _emit("message.complete", sid, {"text": "", "status": "interrupted", "turn_id": turn_id})
                return _ok(rid, {
                    "status": "recalled",
                    "session_id": sid,
                    "stored_session_id": str(session.get("session_key") or ""),
                    "turn_id": turn_id,
                    "interrupted": interrupted,
                    "removed_messages": 0,
                    "draft": draft,
                    "messages": messages,
                    "memory_retract": {
                        "status": "unsupported",
                        "warnings": ["Memory provider turn-level retraction is not implemented yet."],
                    },
                })
            return _err(rid, 4019, "turn not found or already recalled")

        next_history, draft, removed = recalled
        _rewrite_live_and_persisted_history(session, next_history)
        session.setdefault("recalled_turn_ids", set()).add(turn_id)
        if active_turn_id == turn_id or str(session.get("active_turn_id") or "") == turn_id:
            session["running"] = False
            session["active_run_id"] = None
            session["active_turn_id"] = None
            session["pending_turn"] = None
            session["run_updated_at"] = time.time()
        messages = sanitize_transcript_messages(_history_to_messages(next_history))

    _emit("session.recalled", sid, {
        "turn_id": turn_id,
        "removed_messages": removed,
        "draft": draft,
        "messages": messages,
    })
    if interrupted:
        _emit("message.complete", sid, {"text": "", "status": "interrupted", "turn_id": turn_id})
    return _ok(rid, {
        "status": "recalled",
        "session_id": sid,
        "stored_session_id": str(session.get("session_key") or ""),
        "turn_id": turn_id,
        "interrupted": interrupted,
        "removed_messages": removed,
        "draft": draft,
        "messages": messages,
        "memory_retract": {
            "status": "unsupported",
            "warnings": ["Memory provider turn-level retraction is not implemented yet."],
        },
    })


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
    import time as _time

    filename = os.path.abspath(
        f"hermes_conversation_{_time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model": getattr(session["agent"], "model", ""),
                    "messages": session.get("history", []),
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        return _ok(rid, {"file": filename})
    except Exception as e:
        return _err(rid, 5011, str(e))


@method("session.close")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "")
    runtime_sid = sid
    session = _sessions.pop(runtime_sid, None)
    if not session and sid:
        try:
            snapshot = list(_sessions.items())
        except Exception:
            snapshot = []
        for candidate_sid, candidate in snapshot:
            if candidate.get("session_key") == sid:
                runtime_sid = candidate_sid
                session = _sessions.pop(candidate_sid, None)
                break
    if not session:
        return _ok(rid, {"closed": False})
    _finalize_session(session)
    try:
        from tools.approval import unregister_gateway_notify

        unregister_gateway_notify(session["session_key"])
    except Exception:
        pass
    try:
        agent = session.get("agent")
        if agent and hasattr(agent, "close"):
            agent.close()
    except Exception:
        pass
    try:
        worker = session.get("slash_worker")
        if worker:
            worker.close()
    except Exception:
        pass
    return _ok(
        rid,
        {
            "closed": True,
            "session_id": runtime_sid,
            "stored_session_id": session.get("session_key") or sid,
        },
    )


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
            new_key, source="tui", model=_resolve_model(), parent_session_id=old_key
        )
        for msg in history:
            db.append_message(
                session_id=new_key,
                role=msg.get("role", "user"),
                content=msg.get("content"),
            )
        db.set_session_title(new_key, title)
    except Exception as e:
        return _err(rid, 5008, f"branch failed: {e}")
    new_sid = uuid.uuid4().hex[:8]
    try:
        cwd = _session_cwd(session)
        workspace = _workspace_from_params(
            {"workspace": session.get("workspace") or {}},
            cwd,
        )
        workspace = _bind_session_workspace(
            session_id=new_key,
            cwd=cwd,
            workspace=workspace,
        )
        tokens = _set_session_context(new_key, terminal_cwd=cwd)
        try:
            agent = _make_agent(
                new_sid,
                new_key,
                session_id=new_key,
                cwd=cwd,
            )
        finally:
            _clear_session_context(tokens)
        _init_session(
            new_sid,
            new_key,
            agent,
            list(history),
            cols=session.get("cols", 80),
            cwd=cwd,
            workspace=workspace,
        )
    except Exception as e:
        return _err(rid, 5000, f"agent init failed on branch: {e}")
    return _ok(rid, {"session_id": new_sid, "title": title, "parent": old_key})


@method("session.interrupt")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "")
    requested_run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    requested_turn_id = str(params.get("turn_id") or params.get("turnId") or "").strip()
    completion_status = str(
        params.get("completion_status") or params.get("completionStatus") or "interrupted"
    ).strip() or "interrupted"
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    interrupted_run_id = ""
    interrupted_turn_id = ""
    interrupt_seq = 0
    should_interrupt_agent = False
    should_clear_current = False
    with session["history_lock"]:
        active_run_id = str(session.get("active_run_id") or "")
        active_turn_id = str(session.get("active_turn_id") or "")
        interrupted_run_id = requested_run_id or active_run_id
        interrupted_turn_id = requested_turn_id or active_turn_id
        interrupts_current_run = bool(interrupted_run_id and active_run_id == interrupted_run_id)
        interrupts_current_turn = bool(interrupted_turn_id and active_turn_id == interrupted_turn_id)
        has_active_target = bool(active_run_id or active_turn_id)
        has_requested_target = bool(requested_run_id or requested_turn_id)
        should_clear_current = interrupts_current_run or interrupts_current_turn
        should_interrupt_agent = (
            should_clear_current
            or (has_requested_target and not has_active_target)
            or (not has_requested_target and has_active_target)
        )
        session["interrupted_run_id"] = interrupted_run_id
        session["interrupted_turn_id"] = interrupted_turn_id
        session["interrupt_seq"] = int(session.get("interrupt_seq") or 0) + 1
        interrupt_seq = int(session.get("interrupt_seq") or 0)
        if should_clear_current:
            session["running"] = False
            session["active_run_id"] = None
            session["active_turn_id"] = None
            session["run_updated_at"] = time.time()
    _interrupt_trace(
        "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.state "
        f"sid={sid} requested_run_id={requested_run_id or '-'} requested_turn_id={requested_turn_id or '-'} "
        f"active_run_id={active_run_id or '-'} active_turn_id={active_turn_id or '-'} "
        f"interrupted_run_id={interrupted_run_id or '-'} interrupted_turn_id={interrupted_turn_id or '-'} "
        f"should_clear_current={should_clear_current} should_interrupt_agent={should_interrupt_agent} seq={interrupt_seq}",
    )
    _interrupt_trace(
        f"[hermes] [tui_gateway] session.interrupt sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'} seq={interrupt_seq}",
    )
    _request_session_interrupt_side_effects_async(
        sid=sid,
        session=session,
        should_interrupt_agent=should_interrupt_agent,
        interrupted_run_id=interrupted_run_id,
        interrupted_turn_id=interrupted_turn_id,
        completion_status=completion_status,
    )
    _interrupt_trace(
        "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.return "
        f"sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'}",
    )
    return _ok(
        rid,
        {
            "status": "interrupted",
            "run_id": interrupted_run_id,
            "turn_id": interrupted_turn_id,
        },
    )


# ── Delegation: subagent tree observability + controls ───────────────
# Powers the TUI's /agents overlay (see ui-tui/src/components/agentsOverlay).
# The registry lives in tools/delegate_tool — these handlers are thin
# translators between JSON-RPC and the Python API.


@method("delegation.status")
def _(rid, params: dict) -> dict:
    from tools.delegate_tool import (
        is_spawn_paused,
        list_active_subagents,
        _get_max_concurrent_children,
        _get_max_spawn_depth,
    )

    return _ok(
        rid,
        {
            "active": list_active_subagents(),
            "paused": is_spawn_paused(),
            "max_spawn_depth": _get_max_spawn_depth(),
            "max_concurrent_children": _get_max_concurrent_children(),
        },
    )


@method("delegation.pause")
def _(rid, params: dict) -> dict:
    from tools.delegate_tool import set_spawn_paused

    paused = bool(params.get("paused", True))
    return _ok(rid, {"paused": set_spawn_paused(paused)})


@method("subagent.interrupt")
def _(rid, params: dict) -> dict:
    from tools.delegate_tool import interrupt_subagent

    subagent_id = str(params.get("subagent_id") or "").strip()
    if not subagent_id:
        return _err(rid, 4000, "subagent_id required")
    ok = interrupt_subagent(subagent_id)
    return _ok(rid, {"found": ok, "subagent_id": subagent_id})


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


@method("spawn_tree.save")
def _(rid, params: dict) -> dict:
    session_id = str(params.get("session_id") or "").strip()
    subagents = params.get("subagents") or []
    if not isinstance(subagents, list) or not subagents:
        return _err(rid, 4000, "subagents list required")

    from datetime import datetime

    started_at = params.get("started_at")
    finished_at = params.get("finished_at") or time.time()
    label = str(params.get("label") or "")
    ts = datetime.utcfromtimestamp(float(finished_at)).strftime("%Y%m%dT%H%M%S")
    fname = f"{ts}.json"
    d = _spawn_tree_session_dir(session_id or "default")
    path = d / fname
    try:
        payload = {
            "session_id": session_id,
            "started_at": float(started_at) if started_at else None,
            "finished_at": float(finished_at),
            "label": label,
            "subagents": subagents,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return _err(rid, 5000, f"spawn_tree.save failed: {exc}")

    _append_spawn_tree_index(
        d,
        {
            "path": str(path),
            "session_id": session_id,
            "started_at": payload["started_at"],
            "finished_at": payload["finished_at"],
            "label": label,
            "count": len(subagents),
        },
    )

    return _ok(rid, {"path": str(path), "session_id": session_id})


@method("spawn_tree.list")
def _(rid, params: dict) -> dict:
    session_id = str(params.get("session_id") or "").strip()
    limit = int(params.get("limit") or 50)
    cross_session = bool(params.get("cross_session"))

    if cross_session:
        root = _spawn_trees_root()
        roots = [p for p in root.iterdir() if p.is_dir()]
    else:
        roots = [_spawn_tree_session_dir(session_id or "default")]

    entries: list[dict] = []
    for d in roots:
        indexed = _read_spawn_tree_index(d)
        if indexed:
            # Skip index entries whose snapshot file was manually deleted.
            entries.extend(
                e for e in indexed if (p := e.get("path")) and Path(p).exists()
            )
            continue

        # Fallback for legacy (pre-index) sessions: full scan.  O(N) reads
        # but only runs once per session until the next save writes the index.
        for p in d.glob("*.json"):
            if p.name == _SPAWN_TREE_INDEX:
                continue
            try:
                stat = p.stat()
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    raw = {}
                subagents = raw.get("subagents") or []
                entries.append(
                    {
                        "path": str(p),
                        "session_id": raw.get("session_id") or d.name,
                        "finished_at": raw.get("finished_at") or stat.st_mtime,
                        "started_at": raw.get("started_at"),
                        "label": raw.get("label") or "",
                        "count": len(subagents) if isinstance(subagents, list) else 0,
                    }
                )
            except OSError:
                continue

    entries.sort(key=lambda e: e.get("finished_at") or 0, reverse=True)
    return _ok(rid, {"entries": entries[:limit]})


@method("spawn_tree.load")
def _(rid, params: dict) -> dict:
    from pathlib import Path

    raw_path = str(params.get("path") or "").strip()
    if not raw_path:
        return _err(rid, 4000, "path required")

    # Reject paths escaping the spawn-trees root.
    root = _spawn_trees_root().resolve()
    try:
        resolved = Path(raw_path).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError) as exc:
        return _err(rid, 4030, f"path outside spawn-trees root: {exc}")

    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _err(rid, 5000, f"spawn_tree.load failed: {exc}")

    return _ok(rid, payload)


@method("session.steer")
def _(rid, params: dict) -> dict:
    """Inject a user message into the next tool result without interrupting.

    Mirrors AIAgent.steer(). Safe to call while a turn is running — the text
    lands on the last tool result of the next tool batch and the model sees
    it on its next iteration. No interrupt, no new user turn, no role
    alternation violation.
    """
    text = (params.get("text") or "").strip()
    if not text:
        return _err(rid, 4002, "text is required")
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    agent = session.get("agent")
    if agent is None or not hasattr(agent, "steer"):
        return _err(rid, 4010, "agent does not support steer")
    try:
        accepted = agent.steer(text)
    except Exception as exc:
        return _err(rid, 5000, f"steer failed: {exc}")
    return _ok(rid, {"status": "queued" if accepted else "rejected", "text": text})


@method("terminal.resize")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    session["cols"] = int(params.get("cols", 80))
    return _ok(rid, {"cols": session["cols"]})
