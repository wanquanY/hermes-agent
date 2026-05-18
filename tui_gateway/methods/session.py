# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


# ── Methods: session ─────────────────────────────────────────────────


def _stored_workspace(session_id: str) -> dict | None:
    try:
        return _workspace_for_session(session_id)
    except Exception:
        return None


def _session_run_snapshot(runtime_sid: str, session: dict | None) -> dict:
    session = session or {}
    running = bool(session.get("running"))
    return {
        "running": running,
        "active_runtime_session_id": runtime_sid or "",
        "active_run_id": str(session.get("active_run_id") or "") if running else "",
        "active_turn_id": str(session.get("active_turn_id") or "") if running else "",
        "run_started_at": (session.get("run_started_at") or 0) if running else 0,
        "run_updated_at": (session.get("run_updated_at") or 0) if running else 0,
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


def _request_agent_interrupt_async(sid: str, agent) -> None:
    if agent is None or not hasattr(agent, "interrupt"):
        return

    def run_interrupt() -> None:
        try:
            agent.interrupt()
        except Exception as exc:
            print(
                f"[tui_gateway] session.interrupt agent interrupt failed sid={sid}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    threading.Thread(
        target=run_interrupt,
        name=f"tui-session-interrupt-{sid}",
        daemon=True,
    ).start()


@method("session.create")
def _(rid, params: dict) -> dict:
    sid = uuid.uuid4().hex[:8]
    key = _new_session_key()
    cols = int(params.get("cols", 80))
    transient = bool(params.get("transient") or params.get("temporary") or params.get("ephemeral"))
    tool_progress_mode = _requested_tool_progress_mode(params)
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
            "info": {
                "model": _resolve_model(),
                "tools": {},
                "skills": {},
                "cwd": cwd,
                "workspace": workspace,
                "lazy": True,
                "transient": transient,
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
            for s in db.list_sessions_rich(
                source=None,
                limit=fetch_limit,
                order_by_last_active=True,
            )
            if (s.get("source") or "").strip().lower() not in deny
        ][:limit]
        live_by_key = _live_sessions_by_stored_key()
        session_items = []
        for s in rows:
            live_sid, live_session = live_by_key.get(s["id"], ("", None))
            live_state = _session_run_snapshot(live_sid, live_session)
            session_items.append(
                {
                    "id": s["id"],
                    "title": s.get("title") or "",
                    "preview": s.get("preview") or "",
                    "started_at": s.get("started_at") or 0,
                    "updated_at": s.get("last_active") or s.get("started_at") or 0,
                    "message_count": s.get("message_count") or 0,
                    "source": s.get("source") or "",
                    "workspace": _stored_workspace(s["id"]),
                    **live_state,
                }
            )
        return _ok(
            rid,
            {
                "sessions": session_items
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
    try:
        db.reopen_session(target)
        history = db.get_messages_as_conversation(target)
        display_history = db.get_messages_as_conversation(
            target, include_ancestors=True
        )
        messages = _history_to_messages(display_history)
        live_sid, live_session = _resolve_runtime_session(target)
        if live_session is not None:
            live_session["transport"] = (
                current_transport()
                or live_session.get("transport")
                or _stdio_transport
            )
            live_session["cwd"] = cwd
            live_session["workspace"] = workspace
            live_state = _session_run_snapshot(live_sid, live_session)
            return _ok(
                rid,
                {
                    "session_id": live_sid,
                    "resumed": target,
                    "message_count": len(messages),
                    "messages": messages,
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
    except Exception as e:
        return _err(rid, 5000, f"resume failed: {e}")
    return _ok(
        rid,
        {
            "session_id": sid,
            "resumed": target,
            "message_count": len(messages),
            "messages": messages,
            "info": (
                _session_info(agent, _sessions.get(sid))
                if _sessions.get(sid) is not None
                else _session_info(agent)
            ),
            **_session_run_snapshot(sid, _sessions.get(sid)),
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
    runtime_sid, live_session = _resolve_runtime_session(key)
    if live_session is None:
        runtime_sid = str(params.get("session_id") or "")
        live_session = session
    return _ok(
        rid,
        {
            "output": "\n".join(lines),
            "session_id": runtime_sid,
            "stored_session_id": key,
            **_session_run_snapshot(runtime_sid, live_session),
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


@method("session.recall_turn")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "")
    turn_id = str(params.get("turn_id") or "").strip()
    if not turn_id:
        return _err(rid, 4006, "turn_id required")
    session, err = _sess(params, rid)
    if err:
        return err

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
        target_idx = None
        for idx, message in enumerate(history):
            if isinstance(message, dict) and _message_turn_id(message) == turn_id and message.get("role") == "user":
                target_idx = idx
                break
        if target_idx is None:
            if isinstance(pending_turn, dict) and str(pending_turn.get("turn_id") or "") == turn_id:
                draft = _draft_from_turn_message(None, pending_turn)
                session.setdefault("recalled_turn_ids", set()).add(turn_id)
                session["running"] = False
                session["active_run_id"] = None
                session["active_turn_id"] = None
                session["pending_turn"] = None
                session["run_updated_at"] = time.time()
                messages = _history_to_messages(history)
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

        remove_end = len(history)
        for idx in range(target_idx + 1, len(history)):
            message = history[idx]
            if isinstance(message, dict) and message.get("role") == "user":
                remove_end = idx
                break
        target_message = history[target_idx]
        draft = _draft_from_turn_message(target_message, pending_turn)
        next_history = history[:target_idx] + history[remove_end:]
        removed = remove_end - target_idx
        _rewrite_live_and_persisted_history(session, next_history)
        session.setdefault("recalled_turn_ids", set()).add(turn_id)
        if active_turn_id == turn_id or str(session.get("active_turn_id") or "") == turn_id:
            session["running"] = False
            session["active_run_id"] = None
            session["active_turn_id"] = None
            session["pending_turn"] = None
            session["run_updated_at"] = time.time()
        messages = _history_to_messages(next_history)

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
    print(
        f"[tui_gateway] session.interrupt sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'} seq={interrupt_seq}",
        file=sys.stderr,
        flush=True,
    )
    if should_interrupt_agent:
        _request_agent_interrupt_async(sid, session.get("agent"))
    if should_interrupt_agent:
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
    _emit(
        "message.complete",
        sid,
        {
            "text": "",
            "status": "interrupted",
            "run_id": interrupted_run_id,
            "turn_id": interrupted_turn_id,
        },
    )
    with session["history_lock"]:
        if should_clear_current:
            session["running"] = False
            session["active_run_id"] = None
            session["active_turn_id"] = None
            session["run_updated_at"] = time.time()
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
