from __future__ import annotations

import contextvars
from typing import Any

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


_active_profile_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "active_profile_context",
    default=None,
)


def profile_context_for_params(params: dict | None = None) -> dict | None:
    params = params or {}
    profile = params.get("doxie_profile") or params.get("doxieProfile") or params.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    profile_id = str(
        params.get("agent_profile_id")
        or params.get("agentProfileId")
        or profile.get("id")
        or ""
    ).strip()
    version_id = str(
        params.get("agent_profile_version_id")
        or params.get("agentProfileVersionId")
        or profile.get("agentProfileVersionId")
        or profile.get("agent_profile_version_id")
        or ""
    ).strip()
    draft_id = str(
        params.get("agent_profile_draft_id")
        or params.get("agentProfileDraftId")
        or profile.get("draftId")
        or profile.get("draft_id")
        or ""
    ).strip()
    hermes_home = str(
        params.get("hermes_home")
        or params.get("hermesHome")
        or params.get("hermesHomePath")
        or profile.get("hermesHomePath")
        or profile.get("hermes_home")
        or ""
    ).strip()
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or profile.get("runtimeScopeKey")
        or profile.get("runtime_scope_key")
        or ""
    ).strip()
    if not any((profile_id, version_id, draft_id, hermes_home, runtime_scope_key)):
        return None
    if not runtime_scope_key:
        if draft_id:
            runtime_scope_key = f"draft:{draft_id}"
        elif profile_id:
            runtime_scope_key = f"profile:{profile_id}"
    return {
        "id": profile_id,
        "agent_profile_version_id": version_id,
        "agent_profile_draft_id": draft_id,
        "hermes_home": hermes_home,
        "runtime_scope_key": runtime_scope_key,
    }


def enter_profile_context(profile_context: dict | None, *, apply_env: bool = True) -> Any:
    if not profile_context:
        return None
    context_token = _active_profile_context.set(profile_context)
    hermes_home = str(profile_context.get("hermes_home") or "").strip()
    if not apply_env or not hermes_home:
        return context_token
    return [
        ("profile_context", context_token),
        ("hermes_home_override", set_hermes_home_override(hermes_home)),
    ]


def leave_profile_context(token: Any) -> None:
    if isinstance(token, list):
        for item in reversed(token):
            leave_profile_context(item)
        return
    if isinstance(token, tuple) and len(token) == 2:
        kind, inner = token
        if kind == "profile_context":
            _active_profile_context.reset(inner)
            return
        if kind == "hermes_home_override":
            reset_hermes_home_override(inner)
            return
    if token is not None:
        _active_profile_context.reset(token)


def active_hermes_home(*, fallback: str, default_home: str | None = None) -> str:
    profile_context = _active_profile_context.get()
    if isinstance(profile_context, dict) and profile_context.get("hermes_home"):
        return str(profile_context["hermes_home"])
    if default_home is not None:
        return default_home
    try:
        return str(get_hermes_home())
    except Exception:
        return fallback
