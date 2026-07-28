"""Turn-scoped toolset management for TUI gateway sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def normalize_toolsets(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = [raw]

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def normalize_enabled_toolsets(raw: Any) -> list[str]:
    return normalize_toolsets(raw)


def normalize_disabled_toolsets(raw: Any) -> list[str]:
    return normalize_toolsets(raw)


def normalize_toolset_scope(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"exact", "replace", "exclusive"}:
        return "exact"
    return "merge"


def default_public_toolsets() -> list[str]:
    from toolsets import get_all_toolsets, is_internal_toolset

    return [
        name
        for name in sorted(get_all_toolsets().keys())
        if not is_internal_toolset(name)
    ]


def merge_enabled_toolsets(base: list[str] | None, extra: list[str]) -> list[str] | None:
    if not extra:
        return base
    merged = default_public_toolsets() if base is None else list(base)
    seen = set(merged)
    for name in extra:
        if name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


def merge_disabled_toolsets(base: list[str] | None, extra: list[str]) -> list[str] | None:
    if not extra:
        return base
    merged = [] if base is None else list(base)
    seen = set(merged)
    for name in extra:
        if name not in seen:
            seen.add(name)
            merged.append(name)
    return merged or None


def _valid_toolsets(raw: Any) -> list[str]:
    from toolsets import validate_toolset

    return [name for name in normalize_toolsets(raw) if validate_toolset(name)]


def load_session_toolset_overrides(session_id: str) -> dict[str, list[str] | None]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {}
    try:
        from tui_gateway.services.persistence.gateway_store import get_gateway_state_store

        store = get_gateway_state_store(create_if_missing=False)
        payload = store.get_session_toolsets(session_id) if store is not None else None
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    enabled = _valid_toolsets(payload.get("enabled_toolsets"))
    disabled = _valid_toolsets(payload.get("disabled_toolsets"))
    result: dict[str, list[str] | None] = {}
    if payload.get("enabled_toolsets") is not None:
        result["enabled_toolsets"] = enabled
    if payload.get("disabled_toolsets") is not None:
        result["disabled_toolsets"] = disabled or None
    return result


def hydrate_session_toolset_overrides(
    session: dict[str, Any],
    session_id: str,
) -> dict[str, list[str] | None]:
    overrides = load_session_toolset_overrides(session_id)
    if not overrides:
        return {}
    if (
        "enabled_toolsets" in overrides
        and "enabled_toolsets_override" not in session
    ):
        session["enabled_toolsets_override"] = overrides["enabled_toolsets"]
    if (
        "disabled_toolsets" in overrides
        and "disabled_toolsets_override" not in session
    ):
        session["disabled_toolsets_override"] = overrides["disabled_toolsets"]
    return overrides


def resolve_session_toolsets(
    *,
    session: dict[str, Any] | None,
    session_id: str,
    load_enabled_toolsets: Callable[[], list[str] | None],
    load_disabled_toolsets: Callable[[], list[str] | None],
) -> tuple[list[str] | None, list[str] | None]:
    if isinstance(session, dict):
        persisted_toolsets = hydrate_session_toolset_overrides(session, session_id)
    else:
        persisted_toolsets = load_session_toolset_overrides(session_id)
    if isinstance(session, dict) and "enabled_toolsets_override" in session:
        enabled_toolsets = session.get("enabled_toolsets_override")
    elif "enabled_toolsets" in persisted_toolsets:
        enabled_toolsets = persisted_toolsets["enabled_toolsets"]
    else:
        enabled_toolsets = load_enabled_toolsets()
    if isinstance(session, dict) and "disabled_toolsets_override" in session:
        disabled_toolsets = session.get("disabled_toolsets_override")
    elif "disabled_toolsets" in persisted_toolsets:
        disabled_toolsets = persisted_toolsets["disabled_toolsets"]
    else:
        disabled_toolsets = load_disabled_toolsets()
    return enabled_toolsets, disabled_toolsets


def persist_session_toolset_overrides(
    session_id: str,
    *,
    enabled_toolsets: list[str] | None,
    disabled_toolsets: list[str] | None,
) -> None:
    session_id = str(session_id or "").strip()
    if not session_id:
        return
    try:
        from tui_gateway.services.persistence.gateway_store import get_gateway_state_store

        store = get_gateway_state_store(create_if_missing=True)
        if store is not None:
            store.upsert_session_toolsets(
                session_id=session_id,
                enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
            )
    except Exception:
        return


_UNCHANGED = object()


def refresh_agent_tool_filter(
    agent: Any,
    enabled_toolsets: list[str] | None,
    disabled_toolsets: list[str] | None | object = _UNCHANGED,
) -> None:
    from model_tools import get_tool_definitions

    previous_tool_names = set(getattr(agent, "valid_tool_names", None) or set())
    if disabled_toolsets is _UNCHANGED:
        disabled_toolsets = getattr(agent, "disabled_toolsets", None)
    disabled_toolsets = list(disabled_toolsets or []) or None
    agent.enabled_toolsets = enabled_toolsets
    agent.disabled_toolsets = disabled_toolsets
    agent.tools = get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=getattr(agent, "quiet_mode", True),
        tool_search_context_length=getattr(
            getattr(agent, "context_compressor", None),
            "context_length",
            None,
        ),
    )
    agent.valid_tool_names = {
        tool["function"]["name"]
        for tool in agent.tools or []
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    if agent.valid_tool_names != previous_tool_names:
        # The rendered system prompt contains guidance selected from the
        # effective tool names. Keeping the old in-memory snapshot after a
        # tool-surface change sends a contradictory prompt/schema pair.
        agent._cached_system_prompt = None


def ensure_session_turn_toolsets(
    *,
    sid: str,
    session: dict[str, Any],
    requested_toolsets: Any,
    load_enabled_toolsets: Callable[[], list[str] | None],
    emit_session_info: Callable[[str, Any], None],
    requested_disabled_toolsets: Any = None,
    load_disabled_toolsets: Callable[[], list[str] | None] | None = None,
    toolset_scope: Any = None,
    persist_session_id: str | None = None,
) -> None:
    """Apply requested toolset grants/denials to a session's tool surface.

    The default ``merge`` scope preserves the historical behavior: requested
    toolsets are additive grants on top of the session/profile defaults and may
    be persisted for future runtime rebuilds.  Some control-plane runs need an
    authority boundary instead of an additive grant; ``exact`` replaces the
    enabled toolset surface with the requested list for the live runtime only.
    """
    extras = normalize_enabled_toolsets(requested_toolsets)
    disabled_extras = normalize_disabled_toolsets(requested_disabled_toolsets)
    scope = normalize_toolset_scope(toolset_scope)
    if not extras and not disabled_extras and scope != "exact":
        return

    from toolsets import validate_toolset

    valid_extras = [name for name in extras if validate_toolset(name)]
    valid_disabled_extras = [name for name in disabled_extras if validate_toolset(name)]
    if not valid_extras and not valid_disabled_extras and scope != "exact":
        return

    agent = session.get("agent")
    if scope == "exact":
        effective_enabled = list(valid_extras)
        session["enabled_toolsets_override"] = effective_enabled
    else:
        enabled_base = session.get("enabled_toolsets_override")
        if enabled_base is None:
            enabled_base = (
                getattr(agent, "enabled_toolsets", None)
                if agent is not None
                else load_enabled_toolsets()
            )
        effective_enabled = merge_enabled_toolsets(enabled_base, valid_extras)
        if valid_extras:
            session["enabled_toolsets_override"] = effective_enabled

    disabled_base = session.get("disabled_toolsets_override")
    if disabled_base is None:
        disabled_base = (
            getattr(agent, "disabled_toolsets", None)
            if agent is not None
            else (load_disabled_toolsets() if callable(load_disabled_toolsets) else None)
        )
    effective_disabled = merge_disabled_toolsets(disabled_base, valid_disabled_extras)
    if valid_disabled_extras:
        session["disabled_toolsets_override"] = effective_disabled

    if persist_session_id and scope != "exact":
        persist_session_toolset_overrides(
            persist_session_id,
            enabled_toolsets=effective_enabled,
            disabled_toolsets=effective_disabled,
        )

    if agent is None:
        return
    if (
        getattr(agent, "enabled_toolsets", None) == effective_enabled
        and getattr(agent, "disabled_toolsets", None) == effective_disabled
    ):
        return

    refresh_agent_tool_filter(agent, effective_enabled, effective_disabled)
    emit_session_info(sid, agent)
