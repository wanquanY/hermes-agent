# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import contextlib
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
_INDICATOR_DEFAULT = "kaomoji"


def _load_cfg() -> dict:
    try:
        import yaml

        p = Path(getattr(_server, "_hermes_home", _hermes_home)) / "config.yaml"
        mtime = p.stat().st_mtime if p.exists() else None
        cfg_lock = getattr(_server, "_cfg_lock", _cfg_lock)
        with cfg_lock:
            if (
                getattr(_server, "_cfg_cache", None) is not None
                and getattr(_server, "_cfg_mtime", None) == mtime
                and getattr(_server, "_cfg_path", None) == p
            ):
                return copy.deepcopy(getattr(_server, "_cfg_cache"))
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        with cfg_lock:
            _server._cfg_cache = copy.deepcopy(data)
            _server._cfg_mtime = mtime
            _server._cfg_path = p
        return data
    except Exception:
        pass
    return {}


def _save_cfg(cfg: dict):
    import yaml

    path = Path(getattr(_server, "_hermes_home", _hermes_home)) / "config.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
    cfg_lock = getattr(_server, "_cfg_lock", _cfg_lock)
    with cfg_lock:
        _server._cfg_cache = copy.deepcopy(cfg)
        _server._cfg_path = path
        try:
            _server._cfg_mtime = path.stat().st_mtime
        except Exception:
            _server._cfg_mtime = None


def _set_session_context(
    session_key: str,
    *,
    terminal_cwd: str | None = None,
    dovie_product_context: str | None = None,
) -> list:
    try:
        from channels.session_context import set_session_vars

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
        from channels.session_context import clear_session_vars

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
    # the agent's clarify_callback to ``_block(...)`` instead of the
    # worker event-stream path handled by ``WorkerFrameRouter``.
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
    blocking-prompt ``sid`` resolves to. Mirrors the event-stream
    clarify/approval projection path for the Dovie-native ``_block``
    mechanism.

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
        if isinstance(rows, (int, float)) and rows > 0:
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

        init_skin_from_config(_server._load_cfg())
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
    m = _server._load_cfg().get("model", "")
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

        cfg = _server._load_cfg().get("model") or {}
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


def _persisted_session_codex_runtime(session_key: str) -> dict:
    """Read a session's persisted Codex runtime fields from its DB row.

    session.create writes `runtime_executor` / `codex_home` / `codex_extra_env`
    into `sessions.model_config` alongside the model + provider so the runtime
    worker subprocess — which only ever sees the DB row, not the sidecar's
    in-memory session dict — can rebuild the agent with codex_app_server
    api_mode instead of falling through to the openai-codex codex_responses
    path (which then fails on missing OAuth token).

    Returns {} when the row isn't a Codex session or when the DB is
    unreachable; callers treat that as "no override" and follow their
    normal fallback chain.
    """
    key = str(session_key or "").strip()
    if not key:
        return {}
    try:
        db = _db_for_stable_session(key)
        row = db.get_session(key) if db is not None else None
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
    runtime_executor = str(cfg.get("runtime_executor") or "").strip()
    codex_home = str(cfg.get("codex_home") or "").strip()
    codex_extra_env = cfg.get("codex_extra_env")
    if runtime_executor:
        result["runtime_executor"] = runtime_executor
    if codex_home:
        result["codex_home"] = codex_home
    if isinstance(codex_extra_env, dict) and codex_extra_env:
        result["codex_extra_env"] = {
            str(k): str(v) for k, v in codex_extra_env.items() if v is not None
        }
    return result


def _persisted_session_runtime(session_key: str) -> tuple[str, str | None]:
    """Read a session's persisted model + provider from its DB row.

    The control-plane session.create records the composer's per-session model on
    the row, and every /model switch keeps it current (see
    _persist_live_session_runtime / _runtime_model_config). Returning it lets
    _make_agent build a RESUMED or runtime-worker-built agent on the session's
    own model instead of the global config default — so the per-turn /model
    switch becomes a same-model no-op and the model-change marker / slash-worker
    churn never fire. Returns ("", None) when no usable row model is found, so
    callers fall back to the global startup runtime.
    """
    key = str(session_key or "").strip()
    if not key:
        return "", None
    try:
        db = _db_for_stable_session(key)
        row = db.get_session(key) if db is not None else None
    except Exception:
        return "", None
    if not isinstance(row, dict):
        return "", None
    model = str(row.get("model") or "").strip()
    if not model:
        return "", None
    provider = None
    raw_cfg = row.get("model_config")
    cfg = raw_cfg if isinstance(raw_cfg, dict) else None
    if cfg is None and isinstance(raw_cfg, str) and raw_cfg.strip():
        try:
            parsed = json.loads(raw_cfg)
            cfg = parsed if isinstance(parsed, dict) else None
        except Exception:
            cfg = None
    if cfg:
        provider = str(cfg.get("provider") or "").strip() or None
    return model, provider


# Backfilled from upstream 6de3963e3 (#43702) + 7d938cc5c — referenced by the
# absorbed bc4dbce858 model-switch no-op fix, but the underlying helpers landed
# in those two non-P0 commits. Without them /model raises NameError after a
# successful in-place agent swap.

def _runtime_model_config(agent, existing: dict | None = None) -> dict:
    config = dict(existing or {})
    model = str(getattr(agent, "model", "") or "").strip()
    provider = str(getattr(agent, "provider", "") or "").strip()
    base_url = str(getattr(agent, "base_url", "") or "").strip()
    api_mode = str(getattr(agent, "api_mode", "") or "").strip()
    reasoning_config = getattr(agent, "reasoning_config", None)
    service_tier = getattr(agent, "service_tier", None)

    if model:
        config["model"] = model
    if provider:
        config["provider"] = provider
    if base_url:
        config["base_url"] = base_url
    else:
        config.pop("base_url", None)
    if api_mode:
        config["api_mode"] = api_mode
    else:
        config.pop("api_mode", None)
    if isinstance(reasoning_config, dict):
        config["reasoning_config"] = reasoning_config
    else:
        config.pop("reasoning_config", None)
    if service_tier:
        config["service_tier"] = service_tier
    else:
        config.pop("service_tier", None)

    return config


def _persist_live_session_runtime(session: dict | None) -> None:
    """Persist active session runtime so future resumes restore the same footer."""
    if not session:
        return
    agent = session.get("agent")
    session_key = str(session.get("session_key") or "").strip()
    if agent is None or not session_key:
        return

    db = getattr(agent, "_session_db", None) or _get_db()
    if db is None:
        return

    try:
        row = db.get_session(session_key) or {}
        raw_config = row.get("model_config")
        existing_config = {}
        if isinstance(raw_config, dict):
            existing_config = raw_config
        elif isinstance(raw_config, str) and raw_config.strip():
            parsed = json.loads(raw_config)
            if isinstance(parsed, dict):
                existing_config = parsed
        model_config = _runtime_model_config(agent, existing_config)
        model = str(getattr(agent, "model", "") or "").strip()
        if hasattr(db, "update_session_meta"):
            db.update_session_meta(session_key, json.dumps(model_config), model or None)
        elif model and hasattr(db, "update_session_model"):
            db.update_session_model(session_key, model)
    except Exception:
        logger.debug("failed to persist live session runtime", exc_info=True)


def _persist_live_session_system_prompt(session: dict | None) -> None:
    """Refresh the stored system prompt after a live runtime identity change."""
    if not session:
        return
    agent = session.get("agent")
    session_key = str(session.get("session_key") or "").strip()
    if agent is None or not session_key or not hasattr(agent, "_build_system_prompt"):
        return

    db = getattr(agent, "_session_db", None) or _get_db()
    if db is None or not hasattr(db, "update_system_prompt"):
        return

    try:
        prompt = agent._build_system_prompt(None)
        agent._cached_system_prompt = prompt
        prompt_scope_key = _system_prompt_execution_scope_key(session, agent)
        if prompt_scope_key:
            update_scoped = getattr(db, "update_scoped_system_prompt", None)
            if not callable(update_scoped):
                return
            update_scoped(
                getattr(agent, "session_id", None) or session_key,
                prompt_scope_key,
                prompt,
            )
            return
        db.update_system_prompt(getattr(agent, "session_id", None) or session_key, prompt)
    except Exception:
        logger.debug("failed to persist live session system prompt", exc_info=True)


def _system_prompt_execution_scope_key(session: dict, agent: Any) -> str:
    session_key = str(session.get("session_key") or getattr(agent, "session_id", "") or "").strip()
    for context in (
        session.get("run_context"),
        getattr(agent, "run_context", None),
        getattr(agent, "_run_context", None),
    ):
        conversation_session_id = str(
            getattr(context, "conversation_session_id", "") or ""
        ).strip()
        execution_scope_key = str(
            getattr(context, "execution_scope_key", "") or ""
        ).strip()
        if (
            conversation_session_id
            and execution_scope_key
            and conversation_session_id == session_key
            and execution_scope_key != session_key
        ):
            return execution_scope_key
    return ""


def _append_model_switch_marker(session: dict | None, *, model: str, provider: str) -> None:
    """Record a real system-history pivot after a live model switch."""
    if not session:
        return
    session_key = str(session.get("session_key") or "").strip()
    if not session_key:
        return

    # Only emit the marker for a MID-conversation switch. The desktop builds the
    # agent with config.yaml's model.default (e.g. gpt-5.5) then applies the
    # agent profile's real model (e.g. deepseek-v4-pro) on every route into a
    # chat — a genuine gpt-5.5 → deepseek-v4-pro change, but it happens BEFORE
    # the user has sent anything. "[System: The active model for this chat has
    # changed to …]" injected into an empty conversation is pure noise (and
    # confuses the model about a change that never affected any turn). Skip the
    # marker when the conversation has no real user/assistant turn yet; the
    # switch side-effects (worker restart, runtime/system-prompt persist) still
    # ran above, so the model takes effect for the first message regardless.
    history = session.get("history") or []
    has_conversation_turn = any(
        isinstance(entry, dict) and entry.get("role") in ("user", "assistant")
        for entry in history
    )
    if not has_conversation_turn:
        return

    provider_part = f" via provider {provider}" if provider else ""
    marker = (
        "[System: The active model for this chat has changed to "
        f"{model}{provider_part}. From this point forward, use this runtime "
        "metadata when answering questions about what model/provider is active.]"
    )
    entry = {"role": "system", "content": marker}

    lock = session.get("history_lock")
    if lock is not None:
        with lock:
            session.setdefault("history", []).append(entry)
            session["history_version"] = int(session.get("history_version", 0)) + 1
    else:
        session.setdefault("history", []).append(entry)
        session["history_version"] = int(session.get("history_version", 0)) + 1

    try:
        agent = session.get("agent")
        db = getattr(agent, "_session_db", None) if agent is not None else None
        if db is not None:
            db.append_message(session_id=session_key, role="system", content=marker)
            return

        if "_ensure_session_db_row" in globals():
            _ensure_session_db_row(session)
        if "_session_db" in globals():
            with _session_db(session) as scoped_db:
                if scoped_db is not None:
                    scoped_db.append_message(
                        session_id=session_key, role="system", content=marker
                    )
    except Exception:
        logger.debug("failed to persist model switch marker", exc_info=True)



# ─────────────────────────────────────────────────────────────────
# Backfilled from upstream/main:tui_gateway/server.py — all are
# called by absorbed P0/P1 commits but their defining commits
# (god-file Phase 1 / session-cap feature 639c1e363) are not on the
# absorption list, so without these the new-chat hot path raises
# NameError. Verbatim copies; behavior matches upstream main.
# ─────────────────────────────────────────────────────────────────

def _claim_active_session_slot(
    session_key: str,
    *,
    live_session_id: str,
    surface: str = "tui",
) -> tuple[Any, str | None]:
    try:
        from hermes_cli.active_sessions import try_acquire_active_session

        return try_acquire_active_session(
            session_id=session_key,
            surface=surface,
            config=_server._load_cfg(),
            metadata={"live_session_id": live_session_id},
        )
    except Exception as exc:
        logger.warning("Failed to claim active session slot: %s", exc)
        return None, None

def _ensure_session_db_row(session: dict) -> None:
    """Idempotently persist the session's DB row on first real activity.

    Called from prompt.submit so a row only exists once the user actually sends
    a message — abandoned drafts never leave an empty "Untitled" session behind.
    Uses INSERT OR IGNORE under the hood, so re-calls (and the AIAgent's own
    lazy create) are no-ops.

    Only an *explicitly chosen* workspace is persisted as the session's cwd.
    The agent still runs in the auto-detected directory (session["cwd"]), but
    we don't stamp that onto the row — otherwise every session the user never
    picked a folder for gets grouped under whatever directory the desktop
    happened to launch in (e.g. "desktop"). Leaving it null groups them under
    "No workspace", which is the desired default.
    """
    key = session.get("session_key")
    if not key:
        return
    # Persist into the session's own profile db (global remote mode), not the
    # launch profile's — otherwise the row lands in the wrong state.db, the
    # unified list mis-tags it, and resume 404s ("session not found").
    profile_home = session.get("profile_home")
    if profile_home:
        from hermes_state import SessionDB

        try:
            db = SessionDB(db_path=Path(profile_home) / "state.db")
        except Exception:
            logger.debug("failed to open profile db for session row", exc_info=True)
            return
        close_db = True
    else:
        db = _get_db()
        close_db = False
    if db is None:
        return
    # The session's own model/effort/fast pick — the composer override shipped on
    # session.create, or a restored /model switch — must own the row's model +
    # model_config. The agent isn't built yet at first prompt.submit, so derive
    # the row from the live override dict; fall back to the global resolved model
    # only when this chat made no explicit pick. Writing the global default here
    # used to win the INSERT-OR-IGNORE race against the agent's own correct
    # lazy-create, so a reconnect/resume rebuilt from the global model and
    # silently reverted the chat (e.g. picked gpt-5.5, reconnect snapped back to
    # the profile default). model_config carries provider/reasoning/service_tier
    # so resume restores effort + fast too, not just the model name.
    override = session.get("model_override")
    override = override if isinstance(override, dict) else {}
    row_model = str(override.get("model") or "").strip() or _resolve_model()
    model_config: dict = {}
    for src_key, cfg_key in (
        ("model", "model"),
        ("provider", "provider"),
        ("base_url", "base_url"),
        ("api_mode", "api_mode"),
    ):
        if val := override.get(src_key):
            model_config[cfg_key] = str(val)
    # The composer override may carry the RESOLVED provider "custom" for a named
    # ``providers:`` / ``custom_providers:`` entry. Persisting bare "custom" here
    # (the very first DB write for a fresh desktop session, before the agent is
    # built) is the origin of the recurring "No LLM provider configured" rows:
    # on the next resume bare "custom" routes to OpenRouter with no key. Recover
    # the durable ``custom:<name>`` identity from the override's base_url, else
    # the configured provider, so a routable identity is persisted from the
    # start (matches _runtime_model_config's normalization).
    if str(model_config.get("provider") or "").strip().lower() == "custom":
        try:
            from hermes_cli.runtime_provider import canonical_custom_identity

            healed = canonical_custom_identity(
                base_url=model_config.get("base_url") or None
            )
            if healed:
                model_config["provider"] = healed
        except Exception:
            logger.debug(
                "custom provider identity recovery failed (db row)", exc_info=True
            )
    if (reasoning := session.get("create_reasoning_override")) is not None:
        model_config["reasoning_config"] = reasoning
    if tier := session.get("create_service_tier_override"):
        model_config["service_tier"] = tier
    try:
        db.create_session(
            key,
            source=_session_source(session),
            model=row_model,
            model_config=model_config or None,
            cwd=_session_cwd(session) if session.get("explicit_cwd") else None,
        )
    except Exception:
        logger.debug("failed to persist desktop session row", exc_info=True)
    finally:
        if close_db:
            try:
                db.close()
            except Exception:
                pass

@contextlib.contextmanager
def _session_db(session: dict):
    """Yield the SessionDB that owns this session's row (profile-aware).

    Mirrors :func:`_ensure_session_db_row`: a remote/profile session persists
    into its own profile's ``state.db`` (a fresh handle we close on exit);
    everything else borrows the shared ``_get_db()`` handle (left open). Yields
    None when the db is unavailable.
    """
    db, close_db = None, False
    profile_home = session.get("profile_home")
    if profile_home:
        from hermes_state import SessionDB

        try:
            db, close_db = SessionDB(db_path=Path(profile_home) / "state.db"), True
        except Exception:
            logger.debug("failed to open profile db for session", exc_info=True)
    else:
        db = _get_db()
    try:
        yield db
    finally:
        if close_db and db is not None:
            with contextlib.suppress(Exception):
                db.close()

def _git_branch_for_cwd(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch:
                return branch
        head = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return head.stdout.strip() if head.returncode == 0 else ""
    except Exception:
        return ""

def _child_run_active(child_key: str) -> bool:
    ts = _active_child_runs.get(child_key)
    return ts is not None and (time.time() - ts) < _CHILD_RUN_STALE_S

def _completion_cwd(params: dict | None = None) -> str:
    params = params or {}
    raw = (
        params.get("cwd")
        or _sessions.get(params.get("session_id") or "", {}).get("cwd")
        # A session bound to another profile resolves its workspace from THAT
        # profile's config before falling back to the launch profile's env var.
        or _profile_configured_cwd(_profile_home(params.get("profile")))
        or os.environ.get("TERMINAL_CWD")
        or os.getcwd()
    )
    try:
        resolved = os.path.abspath(os.path.expanduser(str(raw)))
        if os.path.isdir(resolved):
            return resolved
    except Exception:
        pass
    return os.getcwd()

def _profile_home(profile: str | None) -> Path | None:
    """Resolve a named profile's home on THIS host, or None for the launch profile."""
    name = (profile or "").strip()
    if not name:
        return None
    try:
        from hermes_cli import profiles as profiles_mod

        home = Path(profiles_mod.get_profile_dir(name))
    except Exception:
        return None
    # Already the launch profile? No override needed.
    if home.resolve() == Path(getattr(_server, "_hermes_home", _hermes_home)).resolve():
        return None
    return home if (home / "state.db").exists() or home.exists() else None

def _coerce_seed_history(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []

    history = []
    for item in value:
        if not isinstance(item, dict):
            continue

        role = item.get("role")
        if role not in ("user", "assistant", "system"):
            continue

        content = item.get("content")
        if content is None:
            content = item.get("text")
        if not isinstance(content, str) or not content.strip():
            continue

        history.append({"role": role, "content": content})

    return history

def _stored_session_runtime_overrides(row: dict | None) -> dict:
    """Return runtime fields persisted with a stored session.

    ``session.resume`` is a session-scoped operation: reopening an older chat
    must restore the model/provider/reasoning state that chat actually used,
    not whatever global model the user most recently selected in another chat.
    The durable session row stores the model directly, the billing provider in
    ``billing_provider``, and richer runtime knobs in JSON ``model_config``.
    """
    if not row:
        return {}

    raw_config = row.get("model_config")
    model_config: dict = {}
    if isinstance(raw_config, dict):
        model_config = raw_config
    elif isinstance(raw_config, str) and raw_config.strip():
        try:
            parsed = json.loads(raw_config)
            if isinstance(parsed, dict):
                model_config = parsed
        except Exception:
            logger.debug("failed to parse stored session model_config", exc_info=True)

    overrides: dict = {}
    model = str(row.get("model") or model_config.get("model") or "").strip()
    # ``billing_provider`` is only the billing bucket — for a custom endpoint it is the
    # bare class ``"custom"``, which agent_init treats as non-routable, so restoring it as
    # the provider override makes ``session.resume`` fail with "No LLM provider configured".
    # Only restore an explicit provider; otherwise leave it unset so resume falls back to
    # the configured default, matching the working CLI path.
    explicit_provider = str(model_config.get("provider") or "").strip()
    billing_provider = str(
        model_config.get("billing_provider") or row.get("billing_provider") or ""
    ).strip()
    provider = explicit_provider
    if not provider and billing_provider.lower() not in _BARE_BILLING_PROVIDERS:
        provider = billing_provider
    base_url = str(model_config.get("base_url") or "").strip()
    api_mode = str(model_config.get("api_mode") or "").strip()
    reasoning_config = model_config.get("reasoning_config")
    service_tier = str(model_config.get("service_tier") or "").strip()

    # Heal a bare ``"custom"`` provider stored by an older build (or any leak
    # site that bypassed _runtime_model_config's normalization). Bare custom is
    # the resolved billing class, not a routable identity — restoring it as the
    # session's provider override routes the resume to the OpenRouter default
    # URL with no api_key, surfacing as "No LLM provider configured". Recover
    # the durable ``custom:<name>`` menu key from the stored base_url, falling
    # back to the configured provider when the row has no base_url (the
    # recurring Desktop/TUI regression vector). If neither names a real entry,
    # drop the bare provider entirely so resume falls back to the configured
    # default rather than the broken OpenRouter route.
    if provider.strip().lower() == "custom":
        healed = None
        try:
            from hermes_cli.runtime_provider import canonical_custom_identity

            healed = canonical_custom_identity(base_url=base_url or None)
        except Exception:
            logger.debug(
                "custom provider identity recovery failed", exc_info=True
            )
        provider = healed or ("" if not base_url else provider)

    if model:
        # Use the same dict-shaped override that live /model switches use so a
        # DB-restored session can preserve custom endpoint metadata across both
        # initial resume and later rebuilds (/new). Deliberately do not persist
        # or restore raw api_key here; endpoint credentials should continue to
        # come from config/env/provider resolution rather than the session DB.
        overrides["model_override"] = {
            "model": model,
            "provider": provider or None,
            "base_url": base_url or None,
            "api_mode": api_mode or None,
        }
    if provider:
        overrides["provider_override"] = provider
    if isinstance(reasoning_config, dict):
        overrides["reasoning_config_override"] = reasoning_config
    if service_tier:
        overrides["service_tier_override"] = service_tier

    return overrides

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

def _profile_configured_cwd(profile_home: Path | None) -> str | None:
    """Resolve a non-launch profile's ``terminal.cwd`` from its own config.yaml.

    The desktop's app-global remote mode serves every profile from one backend,
    so the process-global ``TERMINAL_CWD`` belongs to the *launch* profile. A new
    session bound to another profile must take its workspace from THAT profile's
    config, not the stale env var (issue #40334). Returns an absolute, existing
    directory, or None for placeholders / missing / invalid paths.
    """
    if profile_home is None:
        return None
    try:
        import yaml

        p = Path(profile_home) / "config.yaml"
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw = str((data.get("terminal") or {}).get("cwd") or "").strip()
        if not raw or raw in _CWD_PLACEHOLDERS:
            return None
        resolved = os.path.abspath(os.path.expanduser(raw))
        return resolved if os.path.isdir(resolved) else None
    except Exception:
        return None

def _inflight_snapshot(session: dict) -> dict | None:
    turn = session.get("inflight_turn")
    if not isinstance(turn, dict):
        return None
    user = str(turn.get("user") or "").strip()
    assistant = str(turn.get("assistant") or "")
    streaming = bool(turn.get("streaming"))
    if not user and not assistant and not streaming:
        return None
    return {
        "assistant": assistant,
        "streaming": streaming,
        "user": user,
    }

def _register_session_cwd(session: dict | None) -> None:
    if not session:
        return
    try:
        from tools.terminal_tool import register_task_env_overrides

        register_task_env_overrides(
            session["session_key"], {"cwd": _terminal_task_cwd(session)}
        )
    except Exception:
        pass

def _set_session_cwd(session: dict, cwd: str) -> str:
    resolved = os.path.abspath(os.path.expanduser(str(cwd)))
    if not os.path.isdir(resolved):
        raise ValueError(f"working directory does not exist: {cwd}")
    session["cwd"] = resolved
    # An explicit user choice — persist it as the workspace (and let a later
    # lazy row creation persist it too, not the launch-dir fallback).
    session["explicit_cwd"] = True
    _register_session_cwd(session)
    with _session_db(session) as db:
        if db is not None:
            try:
                db.update_session_cwd(session.get("session_key", ""), resolved)
            except Exception:
                logger.debug("failed to persist session cwd", exc_info=True)
    try:
        from tools.terminal_tool import cleanup_vm

        cleanup_vm(session["session_key"])
    except Exception:
        pass
    return resolved




# ── Round-2 backfill (deps of the round-1 backfilled helpers) ──

_pending_prompt_payloads: dict[str, tuple[str, dict]] = {}
