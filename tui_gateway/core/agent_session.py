# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.interim_message_events import create_interim_assistant_callback

_server = bind_server_globals(globals())
logger = logging.getLogger(__name__)
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


def _truthy_model_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _persisted_session_codex_metadata(session_key: str) -> dict:
    key = str(session_key or "").strip()
    if not key:
        return {}
    try:
        db = _db_for_stable_session(key)
        row = db.sessions.get(key) if db is not None else None
    except Exception:
        return {}
    if not isinstance(row, dict):
        return {}
    raw_cfg = row.get("model_config")
    cfg = raw_cfg if isinstance(raw_cfg, dict) else None
    if cfg is None and isinstance(raw_cfg, str) and raw_cfg.strip():
        try:
            parsed = json.loads(raw_cfg)
            cfg = parsed if isinstance(parsed, dict) else None
        except Exception:
            cfg = None
    if not isinstance(cfg, dict):
        return {}
    result: dict = {}
    mode = str(
        cfg.get("codex_account_mode")
        or cfg.get("codexAccountMode")
        or ""
    ).strip()
    if mode:
        result["codex_account_mode"] = mode
    if "model_explicit" in cfg or "explicit_model" in cfg:
        result["model_explicit"] = _truthy_model_flag(
            cfg.get("model_explicit", cfg.get("explicit_model"))
        )
    return result


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
    callbacks = _tool_event_bridge().agent_callbacks(
        sid,
        block=_block,
        status_update=_status_update,
    )
    if _server._load_interim_assistant_messages():
        callbacks["interim_assistant_callback"] = create_interim_assistant_callback(
            emit=_server._emit,
            session_id=sid,
        )
    return callbacks


def _wire_callbacks(sid: str):
    wire_secret_callbacks(sid, block=_block)
    from tools.project_tools import set_project_workspace_callback

    set_project_workspace_callback(_apply_project_workspace)


def _apply_project_workspace(task_id: str, path: str, _name: str = "") -> None:
    """Move a live GUI session after an intentional project tool action."""
    key = str(task_id or "")
    sid = ""
    session = None
    with _sessions_lock:
        if key in _sessions:
            sid, session = key, _sessions[key]
        else:
            for candidate_sid, candidate in _sessions.items():
                agent = candidate.get("agent")
                if (
                    candidate.get("session_key") == key
                    or getattr(agent, "session_id", None) == key
                ):
                    sid, session = candidate_sid, candidate
                    break
    if session is None:
        return
    try:
        resolved = _set_session_cwd(session, path)
        agent = session.get("agent")
        info = (
            _session_info(agent, session)
            if agent is not None
            else {
                "cwd": resolved,
                "branch": _git_branch_for_cwd(resolved),
                "project": session.get("project"),
                "lazy": True,
            }
        )
        _emit("session.info", sid, info)
    except (OSError, ValueError):
        logger.debug("project workspace move rejected", exc_info=True)


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


def _profile_recommended_skills(profile_context: dict | None) -> list[str]:
    if not isinstance(profile_context, dict):
        return []
    raw = (
        profile_context.get("recommended_skills")
        or profile_context.get("recommendedSkills")
        or []
    )
    if not isinstance(raw, list):
        return []
    skills: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            skills.append(name)
    return skills


def _startup_skill_names(profile_context: dict | None) -> list[str]:
    """Merge profile-owned skill bindings with the explicit CLI override."""

    skills: list[str] = []
    seen: set[str] = set()
    for name in [*_profile_recommended_skills(profile_context), *_parse_tui_skills_env()]:
        if name not in seen:
            seen.add(name)
            skills.append(name)
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
    from tui_gateway.services.pending_prompt_queue import (
        pending_prompt_queue,
        queue_scope_for_db,
    )

    conversation_session_id = str(session.get("session_key") or sid).strip()
    pending_prompt_queue.clear(
        queue_scope_for_db(_db_for_stable_session(conversation_session_id)),
        conversation_session_id,
    )
    tokens = _set_session_context(session["session_key"])
    try:
        # /new is a conversation boundary. Runtime pins belong to the old
        # conversation and the fresh agent must re-derive durable defaults.
        session.pop("model_override", None)
        session.pop("create_reasoning_override", None)
        session.pop("create_service_tier_override", None)
        session.pop("one_turn_model_restore", None)
        new_agent = _make_agent(
            sid,
            session["session_key"],
            session_id=session["session_key"],
        )
    finally:
        _clear_session_context(tokens)
    from tui_gateway.services.model_descriptor import bind_session_agent

    bind_session_agent(session, new_agent)
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
    reasoning_config_override: dict | None = None,
    service_tier_override: str | None = None,
):
    from tui_gateway.synthetic_turn import maybe_build_synthetic_agent

    synthetic = maybe_build_synthetic_agent(
        session_id or key,
        model_override=model_override,
    )
    if synthetic is not None:
        return synthetic

    # Profile workers have their own HERMES_HOME. Reconcile at the last safe
    # boundary before MCP discovery and plugin skill discovery so every new
    # Dovie session sees only the Dovie-owned capability catalog.
    try:
        from dovie_extension.capability_policy import (
            reconcile_managed_dovie_runtime,
        )

        reconcile_managed_dovie_runtime()
    except Exception:
        logger.warning("Dovie profile capability reconciliation failed", exc_info=True)

    # Let fast MCP servers land before AIAgent snapshots the tool registry.
    # This is bounded by config; slow servers are handled by late refresh.
    try:
        from hermes_cli.mcp_startup import wait_for_mcp_discovery

        wait_for_mcp_discovery()
    except Exception:
        pass
    # Legacy embedders may still publish an entry-local discovery thread.
    try:
        from tui_gateway.entry import wait_for_mcp_discovery as wait_for_legacy_mcp

        wait_for_legacy_mcp()
    except Exception:
        pass

    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from tui_gateway.services.runtime_credentials import remember_requested_runtime_provider
    from tui_gateway.services.toolset_scope import resolve_session_toolsets

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

    cfg = _server._load_cfg()
    agent_cfg = cfg.get("agent") or {}
    system_prompt = (agent_cfg.get("system_prompt", "") or "").strip()
    startup_skills = _startup_skill_names(_profile_context)
    loaded_skills: list[str] = []
    missing_skills: list[str] = []
    if startup_skills:
        from agent.skill_commands import build_preloaded_skills_prompt

        skills_prompt, loaded_skills, missing_skills = build_preloaded_skills_prompt(
            startup_skills,
            task_id=session_id or key,
        )
        try:
            from agent.dovie_diagnostics import emit_dovie_diagnostic

            emit_dovie_diagnostic(
                "[profile-skill-binding]",
                {
                    "stage": "agent-build",
                    "agent_profile_id": str(_profile_context.get("id") or ""),
                    "runtime_scope_key": str(
                        _profile_context.get("runtime_scope_key") or ""
                    ),
                    "requested_skills": startup_skills,
                    "loaded_skills": loaded_skills,
                    "missing_skills": missing_skills,
                    "session_id": str(session_id or key),
                },
            )
        except Exception:
            logger.debug("profile skill binding diagnostic failed", exc_info=True)
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
    session_model_descriptor = session_context.get("model_descriptor")
    descriptor_context_window = (
        session_model_descriptor.get("context_window")
        if isinstance(session_model_descriptor, dict)
        else None
    )
    if not (
        isinstance(descriptor_context_window, int)
        and not isinstance(descriptor_context_window, bool)
        and descriptor_context_window > 0
    ):
        descriptor_context_window = None
    def _first_text(*values) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    # Read the row's persisted codex fields once so worker subprocesses (which
    # only see the DB, never the sidecar's in-memory _sessions dict) can pick
    # up runtime_executor / codex_home / codex_extra_env recorded at
    # session.create time. Sidecar in-process callers usually get these from
    # _override or _profile_context and never touch the fallback below.
    try:
        from tui_gateway.core.session_config import (
            _persisted_session_codex_runtime as _persisted_codex_runtime,
        )
        _persisted_codex = _persisted_codex_runtime(session_id or key)
    except Exception as _pc_exc:
        _persisted_codex = {}
        logger.warning("persisted codex runtime lookup failed sid=%s: %s", session_id or key, _pc_exc)
    _persisted_codex_meta = _persisted_session_codex_metadata(session_id or key)
    logger.debug("[codex-flow][_make_agent] ENTER sid=%s override_keys=%s profile_ctx_keys=%s persisted_codex=%s persisted_codex_meta=%s",
        session_id or key,
        sorted((_override or {}).keys()),
        sorted(_profile_context.keys()),
        _persisted_codex,
        _persisted_codex_meta,
    )
    _runtime_executor = _first_text(
        (_override or {}).get("runtime_executor"),
        (_override or {}).get("runtimeExecutor"),
        _profile_context.get("runtime_executor"),
        _profile_context.get("runtimeExecutor"),
        _persisted_codex.get("runtime_executor"),
    )
    _codex_home = _first_text(
        (_override or {}).get("codex_home"),
        (_override or {}).get("codexHome"),
        (_override or {}).get("codexHomePath"),
        _profile_context.get("codex_home"),
        _profile_context.get("codexHome"),
        _profile_context.get("codexHomePath"),
        _persisted_codex.get("codex_home"),
    )
    # Extra env bag for the Codex spawn — used by Dovie to inject the
    # platform runtime token as DOXIE_PLATFORM_API_KEY when the employee is
    # in platform-billing mode. BYO mode sends nothing here, so the codex
    # subprocess falls back to its own ChatGPT auth.json.
    def _first_mapping(*values) -> dict:
        for value in values:
            if isinstance(value, dict) and value:
                return {str(k): str(v) for k, v in value.items() if v is not None}
        return {}
    _codex_extra_env = _first_mapping(
        (_override or {}).get("codex_extra_env"),
        (_override or {}).get("codexExtraEnv"),
        _profile_context.get("codex_extra_env"),
        _profile_context.get("codexExtraEnv"),
        _persisted_codex.get("codex_extra_env"),
    )
    from agent.codex_runtime import normalize_codex_account_mode

    _codex_account_mode = normalize_codex_account_mode(
        _first_text(
            (_override or {}).get("codex_account_mode"),
            (_override or {}).get("codexAccountMode"),
            _profile_context.get("codex_account_mode"),
            _profile_context.get("codexAccountMode"),
            _persisted_codex_meta.get("codex_account_mode"),
        ),
        extra_env=_codex_extra_env,
    )
    _model_explicit = (
        _truthy_model_flag((_override or {}).get("model_explicit"))
        or _truthy_model_flag((_override or {}).get("explicit_model"))
        or _truthy_model_flag(_profile_context.get("model_explicit"))
        or _truthy_model_flag(_profile_context.get("explicit_model"))
        or _truthy_model_flag(_persisted_codex_meta.get("model_explicit"))
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
    from hermes_cli.runtime_provider import _normalize_runtime_executor as _norm_runtime_executor

    normalized_runtime_executor = _norm_runtime_executor(_runtime_executor)
    if normalized_runtime_executor == "codex_app_server":
        # A Codex executor owns the provider boundary because it launches the
        # Codex app-server instead of Hermes' native model client.  Other
        # executor labels (notably Dovie's ordinary ``hermes`` profile value)
        # are orchestration metadata and must never rewrite the configured
        # inference provider to OpenAI Codex.
        requested_provider = _runtime_provider_override or "openai-codex"

    # Guard: refuse to spawn a forced Codex app-server without an isolated
    # employee CODEX_HOME. Silently falling back to the user's ~/.codex would
    # blend platform-employee state with the user's personal Codex account.
    if normalized_runtime_executor == "codex_app_server" and not _codex_home:
        raise ValueError(
            "codex_app_server runtime requires codex_home; refusing to fall back to user home"
        )
    runtime_kwargs = {
        "requested": requested_provider,
        "target_model": model or None,
    }
    _connection_id = _first_text(
        (_override or {}).get("connection_id"),
        (
            (_override or {}).get("model_selection") or {}
        ).get("connection_id")
        if isinstance((_override or {}).get("model_selection"), dict)
        else "",
    )
    if not _connection_id:
        try:
            from tui_gateway.core.session_config import (
                _persisted_session_model_selection,
            )

            _persisted_selection = _persisted_session_model_selection(
                session_id or key
            )
            _connection_id = str(
                _persisted_selection.get("connection_id") or ""
            ).strip()
        except Exception:
            _connection_id = ""
    if _connection_id and _connection_id != "cloud:dovie":
        runtime_kwargs["connection_id"] = _connection_id
    if _runtime_executor:
        runtime_kwargs["runtime_executor"] = _runtime_executor
    if _codex_home:
        runtime_kwargs["codex_home"] = _codex_home
    logger.debug("[codex-flow][_make_agent] pre-resolve runtime_kwargs=%s _runtime_executor=%r _codex_home=%r",
        {k: v for k, v in runtime_kwargs.items() if k != "explicit_api_key"},
        _runtime_executor,
        _codex_home,
    )
    runtime = resolve_runtime_provider(**runtime_kwargs)
    logger.debug("[codex-flow][_make_agent] post-resolve runtime.api_mode=%r runtime.provider=%r runtime.codex_home=%r",
        runtime.get("api_mode"),
        runtime.get("provider"),
        runtime.get("codex_home"),
    )
    # Concrete credentials from a completed in-session /model switch survive the
    # rebuild: when the override carries an explicit base_url / api_key / api_mode
    # (the switch already resolved them), use them verbatim instead of letting
    # resolve_runtime_provider re-derive — re-resolution can return the global
    # endpoint and silently route the session to the wrong provider.
    #
    # EXCEPT for codex_app_server: the runtime dict we just resolved reflects
    # the Codex-employee runtime (spawn a codex CLI subprocess reading its
    # own CODEX_HOME). If the session's override still carries an old
    # base_url / api_mode from a pre-switch chat_completions state (e.g. a
    # persisted `[model_switch]` snapshot), letting them win here silently
    # downgrades the runtime to chat_completions and dies looking for the
    # OpenAI-codex OAuth token. Codex spawn doesn't use base_url / api_key /
    # api_mode at all — they're read by the codex subprocess from
    # CODEX_HOME/config.toml + auth.json.
    _is_codex_app_server = str(runtime.get("api_mode") or "").strip() == "codex_app_server"
    _ov_base_url = str((_override or {}).get("base_url") or "").strip()
    _ov_api_key = (_override or {}).get("api_key")
    _ov_api_mode = str((_override or {}).get("api_mode") or "").strip()
    if _is_codex_app_server:
        _runtime_base_url = runtime.get("base_url")
        _runtime_api_key = runtime.get("api_key")
        _runtime_api_mode = runtime.get("api_mode")
    else:
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
    run_context = session_context.get("run_context")
    memory_session_id = str(
        getattr(run_context, "memory_namespace", "")
        or (
            f"conversation:{getattr(run_context, 'conversation_session_id', '')}"
            f"/participant:{getattr(run_context, 'participant_id', '')}"
            if run_context is not None
            else ""
        )
        or session_id
        or key
    ).strip()
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
        model_context_window=descriptor_context_window,
        reasoning_config=(
            reasoning_config_override
            if reasoning_config_override is not None
            else _load_reasoning_config()
        ),
        service_tier=(
            (None if service_tier_override == "" else service_tier_override)
            if service_tier_override is not None
            else _load_service_tier()
        ),
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        platform="tui",
        session_id=session_id or key,
        memory_session_id=memory_session_id,
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
    agent.preloaded_skills = list(loaded_skills)
    if runtime.get("codex_home") is not None:
        agent.codex_home = runtime.get("codex_home")
    if _codex_extra_env:
        agent.codex_extra_env = _codex_extra_env
    if _is_codex_app_server:
        agent.codex_account_mode = _codex_account_mode
        agent.codex_explicit_model = (
            model if _codex_account_mode == "platform" and _model_explicit else ""
        )
    agent._managed_connection_id = str(runtime.get("connection_id") or "")
    agent._managed_credential_generation = int(
        runtime.get("credential_generation") or 0
    )
    agent._managed_credential_lease_id = str(
        runtime.get("credential_lease_id") or ""
    )
    if agent._managed_connection_id:
        try:
            from hermes_cli.model_connections import ModelConnectionRepository

            _managed_connection = ModelConnectionRepository.for_runtime().get(
                agent._managed_connection_id
            )
        except Exception:
            _managed_connection = None
        agent._managed_credential_ref = str(
            (_managed_connection or {}).get("credential_ref") or ""
        )
    else:
        agent._managed_credential_ref = ""
    remember_requested_runtime_provider(agent, runtime, requested_provider)
    try:
        from agent.credits_tracker import seed_credits_at_session_start

        seed_credits_at_session_start(agent)
    except Exception:
        logger.debug("credits session-start seed failed open", exc_info=True)
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
