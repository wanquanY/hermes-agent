# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


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
                # A lazy session paints the composer before its AIAgent is
                # built. A /model switch must apply to the REAL agent (not just
                # process env), so build it first and wait for it — otherwise
                # _apply_model_switch runs against agent=None and the switch is
                # silently dropped on the next prompt. Skip the build when an
                # explicit provider was given: that resolves a fresh runtime
                # without needing the live agent.
                from hermes_cli.model_switch import parse_model_flags_detailed

                parsed_flags = parse_model_flags_detailed(value)
                explicit_provider = parsed_flags.explicit_provider
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
                _set_session_model_descriptor(
                    session,
                    _normalize_model_descriptor(params.get("model_descriptor") or params.get("modelDescriptor")),
                    clear_if_empty=True,
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
                    "scope": result.get("scope", "session"),
                },
            )
        except Exception as e:
            return _err(rid, 5001, str(e))

    if key == "fast":
        from hermes_cli.session_scope import parse_session_scoped_args

        scoped = parse_session_scoped_args(str(value or ""))
        raw = scoped.value.lower()
        scope = str(params.get("scope") or "").strip().lower()
        global_scope = scope == "global" or scoped.persist_global or session is None
        agent = session.get("agent") if session else None
        if agent is not None:
            current_fast = getattr(agent, "service_tier", None) == "priority"
        elif session is not None and session.get("create_service_tier_override") is not None:
            current_fast = session["create_service_tier_override"] == "priority"
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

            if agent is not None:
                target_model = getattr(agent, "model", None)
            else:
                session_override = (session or {}).get("model_override") or {}
                target_model = (
                    session_override.get("model")
                    if isinstance(session_override, dict)
                    else None
                ) or _resolve_model()
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

        if global_scope:
            _write_config_key("agent.service_tier", nv)
            if session is not None:
                session.pop("create_service_tier_override", None)
        else:
            # Empty string is an explicit-normal sentinel. ``None`` means
            # inherit the profile default during a lazy build or rebuild.
            session["create_service_tier_override"] = (
                "priority" if nv == "fast" else ""
            )
        if agent is not None:
            agent.service_tier = "priority" if nv == "fast" else None
            current_overrides = dict(getattr(agent, "request_overrides", {}) or {})
            current_overrides.pop("service_tier", None)
            current_overrides.pop("speed", None)
            if nv == "fast":
                current_overrides.update(overrides)
            agent.request_overrides = current_overrides
            _persist_live_session_runtime(session)
            _emit(
                "session.info",
                params.get("session_id", ""),
                _session_info(agent, session),
            )
        return _ok(
            rid,
            {
                "key": key,
                "value": nv,
                "scope": "global" if global_scope else "session",
            },
        )

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
        try:
            if session:
                from tools.approval import (
                    disable_session_yolo,
                    enable_session_yolo,
                    is_session_yolo_enabled,
                )

                current = is_session_yolo_enabled(session["session_key"])
                if current:
                    disable_session_yolo(session["session_key"])
                    nv = "0"
                else:
                    enable_session_yolo(session["session_key"])
                    nv = "1"
            else:
                current = is_truthy_value(os.environ.get("HERMES_YOLO_MODE"))
                if current:
                    os.environ.pop("HERMES_YOLO_MODE", None)
                    nv = "0"
                else:
                    os.environ["HERMES_YOLO_MODE"] = "1"
                    nv = "1"
            return _ok(rid, {"key": key, "value": nv})
        except Exception as e:
            return _err(rid, 5001, str(e))

    if key == "reasoning":
        try:
            from hermes_constants import parse_reasoning_effort
            from hermes_cli.session_scope import parse_session_scoped_args

            scoped = parse_session_scoped_args(str(value or ""))
            arg = scoped.value.lower()
            scope = str(params.get("scope") or "").strip().lower()
            global_scope = scope == "global" or scoped.persist_global or session is None
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
            if global_scope:
                _write_config_key("agent.reasoning_effort", arg)
                if session is not None:
                    session.pop("create_reasoning_override", None)
            else:
                session["create_reasoning_override"] = parsed
            if session and session.get("agent") is not None:
                session["agent"].reasoning_config = parsed
                _persist_live_session_runtime(session)
                _emit(
                    "session.info",
                    params.get("session_id", ""),
                    _session_info(session["agent"], session),
                )
            return _ok(
                rid,
                {
                    "key": key,
                    "value": arg,
                    "scope": "global" if global_scope else "session",
                },
            )
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
        raw = str(value or "").strip().lower()
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
                nv = str(value or "default")
                history_reset, info = _apply_personality_to_session(
                    sid_key, session, new_prompt
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

        return _ok(rid, {"home": str(_active_hermes_home()), "display": display_hermes_home()})
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
            {"value": (_load_cfg().get("display") or {}).get("personality", "default")},
        )
    if key == "reasoning":
        cfg = _load_cfg()
        session = _sessions.get(params.get("session_id", ""))
        reasoning_config = None
        if session is not None:
            if isinstance(session.get("create_reasoning_override"), dict):
                reasoning_config = session.get("create_reasoning_override")
            else:
                agent = session.get("agent")
                agent_reasoning = getattr(agent, "reasoning_config", None)
                if isinstance(agent_reasoning, dict):
                    reasoning_config = agent_reasoning
        if isinstance(reasoning_config, dict):
            effort = (
                "none"
                if reasoning_config.get("enabled") is False
                else str(reasoning_config.get("effort") or "medium")
            )
        else:
            raw_effort = (cfg.get("agent") or {}).get("reasoning_effort", "")
            effort = "none" if raw_effort is False else str(raw_effort or "medium")
        display = (
            "show"
            if bool((cfg.get("display") or {}).get("show_reasoning", True))
            else "hide"
        )
        return _ok(rid, {"value": effort, "display": display})
    if key == "fast":
        session = _sessions.get(params.get("session_id", ""))
        tier = None
        if session is not None:
            agent = session.get("agent")
            if session.get("create_service_tier_override") is not None:
                tier = session["create_service_tier_override"]
            elif agent is not None:
                tier = getattr(agent, "service_tier", None)
        if tier is None:
            tier = _load_service_tier()
        return _ok(
            rid,
            {
                "value": "fast" if tier == "priority" else "normal",
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
        cfg_path = Path(_active_hermes_home()) / "config.yaml"
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
