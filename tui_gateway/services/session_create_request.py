"""Normalization helpers for session-create runtime and profile parameters."""

from __future__ import annotations

from typing import Any


def requested_runtime_scope_key(params: dict | None = None) -> str:
    return str(
        (params or {}).get("runtime_scope_key")
        or (params or {}).get("runtimeScopeKey")
        or ""
    ).strip()


def _profile(params: dict) -> dict:
    raw = params.get("dovie_profile") or params.get("dovieProfile") or params.get("profile")
    return raw if isinstance(raw, dict) else {}


def requested_runtime_executor(params: dict | None = None) -> str:
    source = params or {}
    profile = _profile(source)
    return str(
        source.get("runtime_executor")
        or source.get("runtimeExecutor")
        or profile.get("runtime_executor")
        or profile.get("runtimeExecutor")
        or ""
    ).strip()


def requested_codex_home(params: dict | None = None) -> str:
    source = params or {}
    profile = _profile(source)
    return str(
        source.get("codex_home")
        or source.get("codexHome")
        or source.get("codexHomePath")
        or profile.get("codex_home")
        or profile.get("codexHome")
        or profile.get("codexHomePath")
        or ""
    ).strip()


def requested_codex_extra_env(params: dict | None = None) -> dict[str, str]:
    source = params or {}
    profile = _profile(source)
    for candidate in (
        source.get("codex_extra_env"),
        source.get("codexExtraEnv"),
        profile.get("codex_extra_env"),
        profile.get("codexExtraEnv"),
    ):
        if isinstance(candidate, dict) and candidate:
            return {
                str(key): str(value)
                for key, value in candidate.items()
                if value is not None
            }
    return {}


def requested_codex_account_mode(params: dict | None = None) -> str:
    source = params or {}
    profile = _profile(source)
    return str(
        source.get("codex_account_mode")
        or source.get("codexAccountMode")
        or profile.get("codex_account_mode")
        or profile.get("codexAccountMode")
        or ""
    ).strip().lower()


def is_byo_codex_agent(agent: Any) -> bool:
    if agent is None:
        return False
    if str(getattr(agent, "api_mode", "") or "").strip() != "codex_app_server":
        return False
    try:
        from agent.codex_runtime import normalize_codex_account_mode

        account_mode = normalize_codex_account_mode(
            getattr(agent, "codex_account_mode", ""),
            extra_env=getattr(agent, "codex_extra_env", None),
        )
    except Exception:
        return False
    return account_mode == "byo"


def requested_agent_profile_id(params: dict | None = None) -> str:
    return str(
        (params or {}).get("agent_profile_id")
        or (params or {}).get("agentProfileId")
        or ""
    ).strip()


def requested_profile_version_id(params: dict | None = None) -> str:
    return str(
        (params or {}).get("agent_profile_version_id")
        or (params or {}).get("agentProfileVersionId")
        or ""
    ).strip()


def requested_created_by_user_id(params: dict | None = None) -> str:
    source = params or {}
    return str(
        source.get("created_by_user_id")
        or source.get("createdByUserId")
        or source.get("created_by")
        or source.get("createdBy")
        or source.get("user_id")
        or source.get("userId")
        or ""
    ).strip()


__all__ = [
    "is_byo_codex_agent",
    "requested_agent_profile_id",
    "requested_codex_account_mode",
    "requested_codex_extra_env",
    "requested_codex_home",
    "requested_created_by_user_id",
    "requested_profile_version_id",
    "requested_runtime_executor",
    "requested_runtime_scope_key",
]
