# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import copy
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())
_BARE_BILLING_PROVIDERS = {"auto", "openrouter", "custom"}

_CHILD_RUN_STALE_S = 3600.0

_CWD_PLACEHOLDERS = {".", "auto", "cwd"}

_active_child_runs: dict[str, float] = {}

def _session_source(session: dict | None) -> str:
    if session:
        source = str(session.get("source") or "").strip()
        if source:
            return source
    return "tui"

def _terminal_task_cwd(session: dict | None) -> str:
    """Return the cwd that terminal_tool should use for this TUI session.

    ``_completion_cwd`` validates paths on the host so file completion does not
    point at nonsense.  Non-local terminal backends are different: their cwd is
    inside the target environment, so an SSH path like /home/user/workspace may
    not exist on the local macOS host but is still the correct execution cwd.
    """
    backend = (os.environ.get("TERMINAL_ENV") or "").strip().lower()
    if backend and backend != "local":
        raw = os.environ.get("TERMINAL_CWD", "").strip()
        if not raw:
            try:
                terminal_cfg = _server._load_cfg().get("terminal", {})
                if isinstance(terminal_cfg, dict):
                    raw = str(terminal_cfg.get("cwd") or "").strip()
            except Exception:
                raw = ""
        if raw and raw not in {".", "auto", "cwd"}:
            return raw

    return _session_cwd(session)

def _write_config_key(key_path: str, value):
    cfg = _server._load_cfg()
    current = cfg
    keys = key_path.split(".")
    for key in keys[:-1]:
        if key not in current or not isinstance(current.get(key), dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
    _server._save_cfg(cfg)


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
        (_server._load_cfg().get("agent") or {}).get("reasoning_effort", "") or ""
    ).strip()
    return parse_reasoning_effort(effort)


def _load_service_tier() -> str | None:
    raw = (
        str((_server._load_cfg().get("agent") or {}).get("service_tier", "") or "")
        .strip()
        .lower()
    )
    if not raw or raw in {"normal", "default", "standard", "off", "none"}:
        return None
    if raw in {"fast", "priority", "on"}:
        return "priority"
    return None


def _load_show_reasoning() -> bool:
    return bool((_server._load_cfg().get("display") or {}).get("show_reasoning", False))


def _load_tool_progress_mode() -> str:
    env = os.environ.get("HERMES_TUI_TOOL_PROGRESS", "").strip().lower()
    if env in {"off", "new", "all", "verbose"}:
        return env
    raw = (_server._load_cfg().get("display") or {}).get("tool_progress", "all")
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
    raw = (_server._load_cfg().get("agent") or {}).get("disabled_toolsets") or []
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


def _call_restart_slash_worker(sid: str, session: dict) -> None:
    restart = getattr(_server, "_restart_slash_worker", _restart_slash_worker)
    if restart is _call_restart_slash_worker:
        restart = _restart_slash_worker
    restart(sid, session)


def _persist_model_switch(result) -> None:
    from hermes_cli.config import save_config

    cfg = _server._load_cfg()
    model_cfg = cfg.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
        cfg["model"] = model_cfg

    model_cfg["default"] = result.new_model
    # Don't collapse a named custom provider (e.g. dovie-cloud) down to the
    # bare "custom" runtime label. `result.target_provider` is the label the
    # runtime exposes the named endpoint to the Agent as; persisting it would
    # destroy the named-provider binding in config.model.provider and make the
    # next agent build unable to reach the provider's key_env (→ the
    # DOVIE_AUTH_REQUIRED 401). Preserve the existing named provider when the
    # switch stays on the generic "custom" label.
    new_provider = result.target_provider
    if str(new_provider or "").strip().lower() == "custom":
        existing_provider = str(model_cfg.get("provider") or "").strip()
        if existing_provider and existing_provider.lower() not in {"auto", "custom"}:
            new_provider = existing_provider
    model_cfg["provider"] = new_provider
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
    catalog_model_id: str = "",
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
    _agent_api_mode = str(getattr(agent, "api_mode", "") or "").strip() if agent else ""
    _override_for_runtime = session.get("model_override") if isinstance(session, dict) else None
    _override_for_runtime = _override_for_runtime if isinstance(_override_for_runtime, dict) else {}
    _profile_context = session.get("profile_context") if isinstance(session, dict) else None
    _profile_context = _profile_context if isinstance(_profile_context, dict) else {}
    _runtime_executor = str(
        _override_for_runtime.get("runtime_executor")
        or _override_for_runtime.get("runtimeExecutor")
        or _profile_context.get("runtime_executor")
        or _profile_context.get("runtimeExecutor")
        or ""
    ).strip()
    if _agent_api_mode == "codex_app_server" or _runtime_executor == "codex_app_server":
        from agent.codex_runtime import normalize_codex_account_mode

        override = _override_for_runtime
        extra_env = (
            override.get("codex_extra_env")
            or override.get("codexExtraEnv")
            or _profile_context.get("codex_extra_env")
            or _profile_context.get("codexExtraEnv")
            or (getattr(agent, "codex_extra_env", None) if agent else None)
        )
        account_mode = normalize_codex_account_mode(
            override.get("codex_account_mode")
            or override.get("codexAccountMode")
            or _profile_context.get("codex_account_mode")
            or _profile_context.get("codexAccountMode")
            or (getattr(agent, "codex_account_mode", "") if agent else ""),
            extra_env=extra_env,
        )
        if account_mode == "platform":
            next_override = dict(override)
            next_override["model"] = model_input
            next_override["model_explicit"] = True
            next_override["codex_account_mode"] = "platform"
            next_override.setdefault("runtime_executor", "codex_app_server")
            if agent is not None and getattr(agent, "codex_home", None):
                next_override.setdefault("codex_home", getattr(agent, "codex_home"))
            if isinstance(extra_env, dict) and extra_env:
                next_override.setdefault(
                    "codex_extra_env",
                    {str(k): str(v) for k, v in extra_env.items() if v is not None},
                )
            session["model_override"] = next_override
            if agent is not None:
                try:
                    agent.codex_account_mode = "platform"
                    agent.codex_explicit_model = model_input
                    agent.model = model_input
                except Exception:
                    pass
            return {
                "success": True,
                "value": model_input,
                "warning": "",
                "confirm_required": False,
            }
        return {
            "success": True,
            "no_op": True,
            "reason": "codex_employee_owned_runtime",
            "value": getattr(agent, "model", "") or model_input,
        }
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

    # ``model.set`` and prompt RPCs are declarative session state.  Re-applying
    # the model already owned by the live agent is a pure local no-op: do it
    # before credential resolution or optional remote catalog discovery so a
    # route change cannot become network-dependent.  Explicit provider/global
    # CLI commands still enter the full switch pipeline.
    if (
        agent is not None
        and is_session
        and not explicit_provider
        and model_input == str(current_model or "").strip()
    ):
        if pin_session_override:
            session["model_override"] = {
                "model": current_model,
                "provider": (current_provider or None),
                "base_url": (current_base_url or None),
                "api_mode": (getattr(agent, "api_mode", "") or None),
            }
        return {
            "value": current_model,
            "warning": "",
            "confirm_required": False,
        }

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
        catalog_model_id=catalog_model_id,
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
        # Same-model short-circuit: callers (notably the dovie desktop's
        # applyAgentProfileDefaultModel) re-apply a profile's default model on
        # every route into a conversation, with force=True, even when the agent
        # is already on that model. Upstream's _apply_model_switch was designed
        # for the user's manual `/model X` flow where any successful call is a
        # genuine switch, so it unconditionally restarts the slash worker,
        # persists runtime+system-prompt, and appends a "[System: The active
        # model for this chat has changed to ...]" marker into session history.
        # When the model isn't actually changing, those side effects produce:
        #   - a stray system message poisoning every new conversation,
        #   - a slash-worker subprocess churn on every route change,
        #   - redundant db writes and prompt rebuilds.
        # Skip them when the resolved (model, provider) is identical to the
        # currently-running pair. The session model_override / config persist
        # below still run — they are idempotent and let the same-model call
        # serve as a "pin this as the session choice" no-op.
        same_model = (
            str(result.new_model or "").strip() == str(current_model or "").strip()
            and str(result.target_provider or "").strip() == str(current_provider or "").strip()
        )
        if not same_model:
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
            _call_restart_slash_worker(sid, session)
            _persist_live_session_runtime(session)
            _persist_live_session_system_prompt(session)
            _append_model_switch_marker(
                session, model=result.new_model, provider=result.target_provider
            )
            _emit("session.info", sid, _session_info(agent, session))

    # Record the choice as a PER-SESSION override — never as process-global env.
    # The single-process desktop backend shares os.environ across every live
    # session, so writing HERMES_MODEL / HERMES_TUI_PROVIDER here leaks one
    # session's /model switch into every other session's next agent build (the
    # cross-session contamination this path used to cause). _make_agent reads
    # session["model_override"] on the next rebuild (/new keeps the session on
    # its own model), so the env writes are both unnecessary and harmful.
    # api_key is intentionally NOT recorded: the dovie-cloud runtime token
    # rotates, so the rebuild must re-resolve a fresh credential rather than
    # reuse a stale one captured here.
    if session is not None:
        session["model_override"] = {
            "model": result.new_model,
            "provider": (result.target_provider or None),
            "base_url": (result.base_url or None),
            "api_mode": (result.api_mode or None),
        }
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

    AIAgent._compress_context ends the current persisted session and creates
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
                lambda data: _emit_approval_request(sid, data),
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
            _call_restart_slash_worker(sid, session)
        except Exception:
            pass


def _current_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"
