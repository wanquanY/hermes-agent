# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())
_TUI_VERBOSE_TEXT_MAX_CHARS = 16_000
_TUI_VERBOSE_TEXT_MAX_LINES = 240


def _cap_tui_verbose_text(text: str) -> str:
    max_chars = int(getattr(_server, "_TUI_VERBOSE_TEXT_MAX_CHARS", _TUI_VERBOSE_TEXT_MAX_CHARS))
    max_lines = int(getattr(_server, "_TUI_VERBOSE_TEXT_MAX_LINES", _TUI_VERBOSE_TEXT_MAX_LINES))
    if (
        len(text) <= max_chars
        and text.count("\n") < max_lines
    ):
        return text

    idx = len(text)
    start = 0
    for _ in range(max_lines):
        idx = text.rfind("\n", 0, idx)
        if idx < 0:
            start = 0
            break
        start = idx + 1

    line_start = start
    start = max(line_start, len(text) - max_chars)
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
            cfg = cfg or _server._load_cfg()
            return (cfg.get("agent") or {}).get("personalities", {}) or {}


def _validate_personality(value: str, cfg: dict | None = None) -> tuple[str, str]:
    raw = str(value or "").strip()
    name = raw.lower()
    if not name or name in {"none", "default", "neutral"}:
        return "", ""

    personalities = _server._available_personalities(cfg)
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
    cfg = _server._load_cfg()

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
    _server._restart_slash_worker(sid, session)
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
    model_override: dict | None = None,
    profile_context: dict | None = None,
):
    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from tui_gateway.services.runtime_credentials import remember_requested_runtime_provider
    from tui_gateway.services.toolset_scope import resolve_session_toolsets

    cfg = _server._load_cfg()
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
    # Build the agent DIRECTLY on this session's own model when the client
    # shipped one. The desktop composer owns its model as plain UI state and
    # sends it on session.create (stored as session["model_override"]); a
    # resumed chat restores its persisted pick the same way. Honoring it here
    # means each per-profile session starts on its own model with NO post-build
    # /model switch — the switch is what emitted the model-change marker,
    # churned the slash worker, and (via the global config persist) poisoned
    # config.model.provider. Falls back to the global startup runtime when this
    # session made no explicit pick (classic TUI, team/subagent workers).
    # Explicit param (an in-session /model switch hands the completed override
    # straight to the rebuild) wins; otherwise read the live session's stored
    # override (composer pick shipped on session.create).
    _override = model_override if isinstance(model_override, dict) else (_sessions.get(sid) or {}).get("model_override")
    _override = _override if isinstance(_override, dict) else None
    session_context = dict(_sessions.get(sid) or {})
    _profile_context = (
        profile_context
        if isinstance(profile_context, dict)
        else session_context.get("profile_context")
    )
    if not isinstance(_profile_context, dict):
        try:
            from tui_gateway.services.profile_context import active_profile_context

            _profile_context = active_profile_context()
        except Exception:
            _profile_context = None
    _profile_context = _profile_context if isinstance(_profile_context, dict) else {}

    def _first_text(*values) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    _runtime_executor = _first_text(
        (_override or {}).get("runtime_executor"),
        (_override or {}).get("runtimeExecutor"),
        _profile_context.get("runtime_executor"),
        _profile_context.get("runtimeExecutor"),
    )
    _codex_home = _first_text(
        (_override or {}).get("codex_home"),
        (_override or {}).get("codexHome"),
        (_override or {}).get("codexHomePath"),
        _profile_context.get("codex_home"),
        _profile_context.get("codexHome"),
        _profile_context.get("codexHomePath"),
    )
    _runtime_provider_override = _first_text(
        (_override or {}).get("provider"),
        _profile_context.get("provider"),
        _profile_context.get("model_provider"),
        _profile_context.get("modelProvider"),
    )
    _override_model = str((_override or {}).get("model") or "").strip()
    if _override_model:
        model = _override_model
        requested_provider = str((_override or {}).get("provider") or "").strip() or None
    else:
        # No live composer override (e.g. a resumed conversation or a runtime
        # worker rebuilding an agent for a control-plane session) — build on the
        # session's PERSISTED model so it starts on its own model, not the
        # global default. This is what closes the control-plane → runtime-worker
        # gap: the desktop's model, recorded on the row at session.create, now
        # reaches _make_agent here so the per-turn /model switch is a no-op.
        _persisted_model, _persisted_provider = _persisted_session_runtime(session_id or key)
        if _persisted_model:
            model = _persisted_model
            requested_provider = _persisted_provider
        else:
            model, requested_provider = _resolve_startup_runtime()
    if _runtime_executor:
        requested_provider = _runtime_provider_override or "openai-codex"
    # Guard: refuse to spawn a forced Codex app-server without an isolated
    # employee CODEX_HOME. Silently falling back to the user's ~/.codex would
    # blend platform-employee state with the user's personal Codex account.
    from hermes_cli.runtime_provider import _normalize_runtime_executor as _norm_runtime_executor
    if _norm_runtime_executor(_runtime_executor) == "codex_app_server" and not _codex_home:
        raise ValueError(
            "codex_app_server runtime requires codex_home; refusing to fall back to user home"
        )
    runtime_kwargs = {
        "requested": requested_provider,
        "target_model": model or None,
    }
    if _runtime_executor:
        runtime_kwargs["runtime_executor"] = _runtime_executor
    if _codex_home:
        runtime_kwargs["codex_home"] = _codex_home
    runtime = resolve_runtime_provider(**runtime_kwargs)
    # Concrete credentials from a completed in-session /model switch survive the
    # rebuild: when the override carries an explicit base_url / api_key / api_mode
    # (the switch already resolved them), use them verbatim instead of letting
    # resolve_runtime_provider re-derive — re-resolution can return the global
    # endpoint and silently route the session to the wrong provider.
    _ov_base_url = str((_override or {}).get("base_url") or "").strip()
    _ov_api_key = (_override or {}).get("api_key")
    _ov_api_mode = str((_override or {}).get("api_mode") or "").strip()
    _runtime_base_url = _ov_base_url or runtime.get("base_url")
    _runtime_api_key = _ov_api_key if (isinstance(_ov_api_key, str) and _ov_api_key.strip()) else runtime.get("api_key")
    _runtime_api_mode = _ov_api_mode or runtime.get("api_mode")
    enabled_toolsets, disabled_toolsets = resolve_session_toolsets(
        session=_sessions.get(sid),
        session_id=session_id or key,
        load_enabled_toolsets=_load_enabled_toolsets,
        load_disabled_toolsets=_load_disabled_toolsets,
    )
    if agent_context_mode:
        session_context["agent_context_mode"] = agent_context_mode
    context_options = _agent_context_options_for_session(session_context)
    agent = AIAgent(
        model=model,
        max_iterations=_cfg_max_turns(cfg, 90),
        provider=runtime.get("provider"),
        base_url=_runtime_base_url,
        api_key=_runtime_api_key,
        api_mode=_runtime_api_mode,
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
    if runtime.get("codex_home") is not None:
        agent.codex_home = runtime.get("codex_home")
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

        register_gateway_notify(key, lambda data: _emit_approval_request(sid, data))
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
