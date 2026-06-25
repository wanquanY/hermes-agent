# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


# ── Methods: tools & system ──────────────────────────────────────────


@method("gateway.capabilities")
def _(rid, params: dict) -> dict:
    from dovie_extension.manifest import gateway_capabilities

    return _ok(rid, gateway_capabilities())


def _profile_runtime_scope_from_params(params: dict) -> dict:
    agent_profile_id = str(
        params.get("agent_profile_id")
        or params.get("agentProfileId")
        or params.get("profile_id")
        or params.get("profileId")
        or ""
    ).strip()
    agent_profile_version_id = str(
        params.get("agent_profile_version_id")
        or params.get("agentProfileVersionId")
        or params.get("version_id")
        or params.get("versionId")
        or ""
    ).strip()
    draft_id = str(
        params.get("agent_profile_draft_id")
        or params.get("agentProfileDraftId")
        or params.get("draft_id")
        or params.get("draftId")
        or ""
    ).strip()
    explicit_scope = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or ""
    ).strip()
    if explicit_scope:
        runtime_scope_key = explicit_scope
    elif draft_id:
        runtime_scope_key = f"draft:{draft_id}"
    elif agent_profile_id:
        runtime_scope_key = f"profile:{agent_profile_id}"
    else:
        runtime_scope_key = "profile:agent-default"
    return {
        "agent_profile_id": agent_profile_id,
        "agent_profile_version_id": agent_profile_version_id,
        "agent_profile_draft_id": draft_id,
        "runtime_scope_key": runtime_scope_key,
        "transient": bool(draft_id or str(runtime_scope_key).startswith("draft:")),
    }


@method("profile.prepare_runtime")
def _(rid, params: dict) -> dict:
    """Normalize the Dovie profile runtime scope before a worker is used.

    Dovie owns profile metadata and filesystem preparation. Hermes owns the
    stable Gateway ABI for profile-scoped runtime identity. This control-plane
    method gives clients a side-effect-light contract check that does not build
    an agent or touch model/tool state.
    """
    scope = _profile_runtime_scope_from_params(params or {})
    return _ok(
        rid,
        {
            "status": "prepared",
            "prepared": True,
            **scope,
        },
    )


@method("runtime.ensure")
def _(rid, params: dict) -> dict:
    """Phase 6 stub: kept for frontend compatibility.

    The legacy worker pre-warm path (spawning a sub-sidecar) is gone.
    The new ``WorkerSupervisor`` spawns workers lazily on the first
    ``run.submit`` for a scope, so ``runtime.ensure`` has no real work
    to do — just acknowledge the scope and return ready=True. Frontend
    runtime readiness machinery (``DesktopSessionRuntime`` etc.) still
    calls this method on session attach as a liveness check."""
    scope = _profile_runtime_scope_from_params(params or {})
    return _ok(
        rid,
        {
            "status": "ready",
            "ready": True,
            **scope,
        },
    )


@method("runtime.status")
def _(rid, params: dict) -> dict:
    """Return lightweight gateway runtime diagnostics without building an agent."""
    try:
        from gateway.status import read_runtime_status

        state = read_runtime_status()
        if not isinstance(state, dict):
            state = {}
    except Exception as exc:
        state = {}
        error = str(exc)
    else:
        error = ""
    try:
        from tui_gateway.services.worker_runtime import worker_supervisor

        runtime_proxy = worker_supervisor().snapshot()
    except Exception as exc:
        runtime_proxy = {"error": str(exc)}
    return _ok(
        rid,
        {
            "status": str(state.get("gateway_state") or state.get("status") or "unknown"),
            "runtime": state,
            "runtime_proxy": runtime_proxy,
            "available": bool(state),
            **({"error": error} if error else {}),
        },
    )


@method("storage.stats")
def _(rid, params: dict) -> dict:
    """Return read-only Hermes storage diagnostics."""
    try:
        from tui_gateway.services.storage_stats import collect_storage_stats

        include_file_sizes = params.get("include_registered_file_sizes")
        if include_file_sizes is None:
            include_file_sizes = params.get("includeRegisteredFileSizes")
        if include_file_sizes is None:
            include_file_sizes = "true"
        result = collect_storage_stats(
            include_registered_file_sizes=str(include_file_sizes).strip().lower() not in {"0", "false", "no"},
            registered_file_limit=int(
                params.get("registered_file_limit")
                or params.get("registeredFileLimit")
                or 10_000
            ),
        )
        return _ok(rid, result)
    except Exception as exc:
        return _err(rid, 5021, f"storage stats failed: {exc}")


@method("process.stop")
def _(rid, params: dict) -> dict:
    try:
        from tools.process_registry import process_registry

        return _ok(rid, {"killed": process_registry.kill_all()})
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
            # picks up added/removed MCP tools without `/new`. The agent
            # snapshots tools once at build and never re-reads the registry, so
            # an explicit rebuild — re-resolving enabled toolsets so a server
            # the user just enabled this session is actually picked up — is
            # required. Mirrors gateway/run.py::_execute_mcp_reload.
            try:
                from tools.mcp_tool import refresh_agent_mcp_tools

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
            _emit("session.info", params.get("session_id", ""), _session_info(agent, session))

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


@method("commands.catalog")
def _(rid, params: dict) -> dict:
    """Registry-backed slash metadata for the TUI — categorized, no aliases."""
    try:
        from hermes_cli.commands import (
            COMMAND_REGISTRY,
            SUBCOMMANDS,
            _build_description,
        )

        all_pairs: list[list[str]] = []
        canon: dict[str, str] = {}
        categories: list[dict] = []
        cat_map: dict[str, list[list[str]]] = {}
        cat_order: list[str] = []

        for cmd in COMMAND_REGISTRY:
            if cmd.name in _TUI_HIDDEN or cmd.gateway_only:
                continue

            c = f"/{cmd.name}"
            canon[c.lower()] = c
            for a in cmd.aliases:
                canon[f"/{a}".lower()] = c

            desc = _build_description(cmd)
            all_pairs.append([c, desc])

            cat = cmd.category
            if cat not in cat_map:
                cat_map[cat] = []
                cat_order.append(cat)
            cat_map[cat].append([c, desc])

        for name, desc, cat in _TUI_EXTRA:
            all_pairs.append([name, desc])
            if cat not in cat_map:
                cat_map[cat] = []
                cat_order.append(cat)
            cat_map[cat].append([name, desc])

        warning = ""
        try:
            qcmds = _load_cfg().get("quick_commands", {}) or {}
            if isinstance(qcmds, dict) and qcmds:
                bucket = "User commands"
                if bucket not in cat_map:
                    cat_map[bucket] = []
                    cat_order.append(bucket)
                for qname, qc in sorted(qcmds.items()):
                    if not isinstance(qc, dict):
                        continue
                    key = f"/{qname}"
                    canon[key.lower()] = key
                    qtype = qc.get("type", "")
                    if qtype == "exec":
                        default_desc = f"exec: {qc.get('command', '')}"
                    elif qtype == "alias":
                        default_desc = f"alias → {qc.get('target', '')}"
                    else:
                        default_desc = qtype or "quick command"
                    qdesc = str(qc.get("description") or default_desc)
                    qdesc = qdesc[:120] + ("…" if len(qdesc) > 120 else "")
                    all_pairs.append([key, qdesc])
                    cat_map[bucket].append([key, qdesc])
        except Exception as e:
            if not warning:
                warning = f"quick_commands discovery unavailable: {e}"

        skill_count = 0
        try:
            from agent.skill_commands import scan_skill_commands

            for k, info in sorted(scan_skill_commands().items()):
                d = str(info.get("description", "Skill"))
                all_pairs.append([k, d[:120] + ("…" if len(d) > 120 else "")])
                skill_count += 1
        except Exception as e:
            warning = f"skill discovery unavailable: {e}"

        for cat in cat_order:
            categories.append({"name": cat, "pairs": cat_map[cat]})

        sub = {k: v[:] for k, v in SUBCOMMANDS.items()}
        return _ok(
            rid,
            {
                "pairs": all_pairs,
                "sub": sub,
                "canon": canon,
                "categories": categories,
                "skill_count": skill_count,
                "warning": warning,
            },
        )
    except Exception as e:
        return _err(rid, 5020, str(e))


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


@method("cli.exec")
def _(rid, params: dict) -> dict:
    """Run `python -m hermes_cli.main` with argv; capture stdout/stderr (non-interactive only)."""
    argv = params.get("argv", [])
    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        return _err(rid, 4003, "argv must be list[str]")
    hint = _cli_exec_blocked(argv)
    if hint:
        return _ok(rid, {"blocked": True, "hint": hint, "code": -1, "output": ""})
    try:
        r = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", *argv],
            capture_output=True,
            text=True,
            timeout=min(int(params.get("timeout", 240)), 600),
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
        parts = [r.stdout or "", r.stderr or ""]
        out = "\n".join(p for p in parts if p).strip() or "(no output)"
        return _ok(
            rid, {"blocked": False, "code": r.returncode, "output": out[:48_000]}
        )
    except subprocess.TimeoutExpired:
        return _err(rid, 5016, "cli.exec: timeout")
    except Exception as e:
        return _err(rid, 5017, str(e))


@method("command.resolve")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.commands import resolve_command

        r = resolve_command(params.get("name", ""))
        if r:
            return _ok(
                rid,
                {
                    "canonical": r.name,
                    "description": r.description,
                    "category": r.category,
                },
            )
        return _err(rid, 4011, f"unknown command: {params.get('name')}")
    except Exception as e:
        return _err(rid, 5012, str(e))


def _resolve_name(name: str) -> str:
    try:
        from hermes_cli.commands import resolve_command

        r = resolve_command(name)
        return r.name if r else name
    except Exception:
        return name


def _prefill_text_from_content(content) -> str:
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(text for text in parts if text)
    return content if isinstance(content, str) else ""


def _dispatch_rewind_command(rid, session: dict | None, name: str, arg: str) -> dict:
    label = "undo" if name == "undo" else "rewind"
    if not session:
        return _err(rid, 4001, f"no active session to {label}")
    if session.get("running"):
        return _err(
            rid,
            4009,
            f"session busy — /interrupt the current turn before /{label}",
        )
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    session_key = str(session.get("session_key") or "")
    if not session_key:
        return _err(rid, 4001, f"no session key for {label}")

    count = 1
    arg_text = (arg or "").strip()
    if arg_text:
        try:
            count = int(arg_text.split()[0])
        except (ValueError, IndexError):
            return _err(
                rid,
                4004,
                f"{label}: invalid count {arg_text!r} — use /{label} or /{label} N",
            )
    count = max(count, 1)

    try:
        recents = db.list_recent_user_messages(session_key, limit=max(count, 10))
    except Exception as exc:
        return _err(rid, 5008, f"{label}: failed to load history: {exc}")
    if not recents:
        return _err(rid, 4018, f"no user messages to {label}")

    target_index = min(count - 1, len(recents) - 1)
    target_id = recents[target_index]["id"]
    try:
        result = db.rewind_to_message(session_key, target_id)
    except ValueError as exc:
        return _err(rid, 4004, f"{label}: {exc}")
    except Exception as exc:
        return _err(rid, 5008, f"{label}: {exc}")

    try:
        active_history = db.get_messages_as_conversation(
            session_key,
            include_ancestors=False,
        )
    except Exception:
        active_history = []
    history_lock = session.get("history_lock")
    if history_lock is None:
        return _err(rid, 5008, f"{label}: session history lock unavailable")
    with history_lock:
        session["history"] = list(active_history)
        session["history_version"] = int(session.get("history_version", 0)) + 1

    agent = session.get("agent")
    if agent is not None:
        memory_manager = getattr(agent, "_memory_manager", None)
        if memory_manager is not None:
            try:
                memory_manager.on_session_switch(
                    session_key,
                    parent_session_id="",
                    reset=False,
                    rewound=True,
                )
            except Exception:
                pass
        for attr in ("_invalidate_system_prompt",):
            fn = getattr(agent, attr, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        try:
            agent._session_messages = list(active_history)
        except Exception:
            pass
        try:
            agent._last_flushed_db_idx = len(active_history)
        except Exception:
            pass
        try:
            agent._cached_system_prompt = None
        except Exception:
            pass

    target_message = result.get("target_message") or {}
    target_text = _prefill_text_from_content(target_message.get("content"))
    rewound_count = int(result.get("rewound_count") or 0)
    turns = target_index + 1
    if label == "undo":
        turn_word = "turn" if turns == 1 else "turns"
        notice = (
            f"↶ Undid {turns} {turn_word} ({rewound_count} message(s)). "
            "Edit and resubmit, or send a new message."
        )
    else:
        notice = (
            f"↶ Rewound {rewound_count} message(s). "
            "Edit and resubmit, or send a new message."
        )
    return _ok(rid, {"type": "prefill", "message": target_text, "notice": notice})


@method("command.dispatch")
def _(rid, params: dict) -> dict:
    name, arg = params.get("name", "").lstrip("/"), params.get("arg", "")
    resolved = _resolve_name(name)
    if resolved != name:
        name = resolved
    session = _sessions.get(params.get("session_id", ""))

    qcmds = _load_cfg().get("quick_commands", {})
    if name in qcmds:
        qc = qcmds[name]
        if qc.get("type") == "exec":
            r = subprocess.run(
                qc.get("command", ""),
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = (
                (r.stdout or "")
                + ("\n" if r.stdout and r.stderr else "")
                + (r.stderr or "")
            ).strip()[:4000]
            if r.returncode != 0:
                return _err(
                    rid,
                    4018,
                    output or f"quick command failed with exit code {r.returncode}",
                )
            return _ok(rid, {"type": "exec", "output": output})
        if qc.get("type") == "alias":
            return _ok(rid, {"type": "alias", "target": qc.get("target", "")})

    try:
        from hermes_cli.plugins import (
            get_plugin_command_handler,
            resolve_plugin_command_result,
        )

        handler = get_plugin_command_handler(name)
        if handler:
            result = resolve_plugin_command_result(handler(arg))
            return _ok(rid, {"type": "plugin", "output": str(result or "")})
    except Exception:
        pass

    try:
        from agent.skill_commands import (
            scan_skill_commands,
            build_skill_invocation_message,
        )

        cmds = scan_skill_commands()
        key = f"/{name}"
        if key in cmds:
            msg = build_skill_invocation_message(
                key, arg, task_id=session.get("session_key", "") if session else ""
            )
            if msg:
                return _ok(
                    rid,
                    {
                        "type": "skill",
                        "message": msg,
                        "name": cmds[key].get("name", name),
                    },
                )
    except Exception:
        pass

    # ── Commands that queue messages onto _pending_input in the CLI ───
    # In the TUI the slash worker subprocess has no reader for that queue,
    # so we handle them here and return a structured payload.

    if name in {"queue", "q"}:
        if not arg:
            return _err(rid, 4004, "usage: /queue <prompt>")
        return _ok(rid, {"type": "send", "message": arg})

    if name == "retry":
        if not session:
            return _err(rid, 4001, "no active session to retry")
        if session.get("running"):
            return _err(
                rid, 4009, "session busy — /interrupt the current turn before /retry"
            )
        history = session.get("history", [])
        if not history:
            return _err(rid, 4018, "no previous user message to retry")
        # Walk backwards to find the last user message
        last_user_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            return _err(rid, 4018, "no previous user message to retry")
        content = history[last_user_idx].get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not content:
            return _err(rid, 4018, "last user message is empty")
        # Truncate history: remove everything from the last user message onward
        # (mirrors CLI retry_last() which strips the failed exchange)
        with session["history_lock"]:
            session["history"] = history[:last_user_idx]
            session["history_version"] = int(session.get("history_version", 0)) + 1
        return _ok(rid, {"type": "send", "message": content})

    if name == "steer":
        if not arg:
            return _err(rid, 4004, "usage: /steer <prompt>")
        agent = session.get("agent") if session else None
        if agent and hasattr(agent, "steer"):
            try:
                accepted = agent.steer(arg)
                if accepted:
                    return _ok(
                        rid,
                        {
                            "type": "exec",
                            "output": f"⏩ Steer queued — arrives after the next tool call: {arg[:80]}{'...' if len(arg) > 80 else ''}",
                        },
                    )
            except Exception:
                pass
        # Fallback: no active run, treat as next-turn message
        return _ok(rid, {"type": "send", "message": arg})

    if name == "goal":
        if not session:
            return _err(rid, 4001, "no active session")
        try:
            from hermes_cli.goals import GoalManager
        except Exception as exc:
            return _err(rid, 5030, f"goals unavailable: {exc}")

        sid_key = session.get("session_key") or ""
        if not sid_key:
            return _err(rid, 4001, "no session key")

        try:
            goals_cfg = _load_cfg().get("goals") or {}
            max_turns = int(goals_cfg.get("max_turns", 20) or 20)
        except Exception:
            max_turns = 20
        mgr = GoalManager(session_id=sid_key, default_max_turns=max_turns)

        lower = arg.strip().lower()
        if not arg.strip() or lower == "status":
            return _ok(rid, {"type": "exec", "output": mgr.status_line()})
        if lower == "pause":
            state = mgr.pause(reason="user-paused")
            out = "No goal set." if state is None else f"⏸ Goal paused: {state.goal}"
            return _ok(rid, {"type": "exec", "output": out})
        if lower == "resume":
            state = mgr.resume()
            if state is None:
                return _ok(rid, {"type": "exec", "output": "No goal to resume."})
            return _ok(
                rid,
                {
                    "type": "exec",
                    "output": (
                        f"▶ Goal resumed: {state.goal}\n"
                        "Send any message to continue, or wait — I'll take the next step on the next turn."
                    ),
                },
            )
        if lower in {"clear", "stop", "done"}:
            had = mgr.has_goal()
            mgr.clear()
            return _ok(
                rid,
                {
                    "type": "exec",
                    "output": "✓ Goal cleared." if had else "No active goal.",
                },
            )

        # Otherwise — treat the remaining text as the new goal.
        try:
            state = mgr.set(arg)
        except ValueError as exc:
            return _err(rid, 4004, f"invalid goal: {exc}")

        notice = (
            f"⊙ Goal set ({state.max_turns}-turn budget): {state.goal}\n"
            "I'll keep working until the goal is done, you pause/clear it, or the budget is exhausted.\n"
            "Controls: /goal status · /goal pause · /goal resume · /goal clear"
        )
        # Send the goal text as the kickoff prompt. The TUI client sees
        # {type: send, notice, message} → renders `notice` as a sys line,
        # then submits `message` as a user turn. The post-turn judge
        # wired in _run_prompt_submit takes over from there.
        return _ok(
            rid,
            {"type": "send", "notice": notice, "message": state.goal},
        )

    if name in {"undo", "rewind"}:
        return _dispatch_rewind_command(rid, session, name, arg)

    if name in {"snapshot", "snap"}:
        subcommand = arg.split(maxsplit=1)[0].lower() if arg else ""
        if subcommand in {"restore", "rewind"}:
            return _ok(
                rid,
                {
                    "type": "exec",
                    "output": (
                        "/snapshot restore is blocked in the TUI because it changes "
                        "config/state on disk while the live agent has cached settings. "
                        "Run it in the classic CLI, then restart the TUI."
                    ),
                },
            )

    return _err(rid, 4018, f"not a quick/plugin/skill command: {name}")
