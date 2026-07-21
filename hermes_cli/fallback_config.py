"""Canonical fallback provider-chain normalization and credential lookup."""

from __future__ import annotations

from typing import Any

from agent.secret_scope import get_profile_env


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """Resolve inline or profile-scoped env credentials for one route."""
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    env_name = str(
        entry.get("key_env") or entry.get("api_key_env") or ""
    ).strip()
    if not env_name:
        return None
    return str(get_profile_env(env_name) or "").strip() or None


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Merge valid new and legacy fallback routes, preserving order."""
    source = config if isinstance(config, dict) else {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in ("fallback_providers", "fallback_model"):
        for entry in _entries(source.get(key)):
            identity = (
                entry["provider"].lower(),
                entry["model"].lower(),
                str(entry.get("base_url") or "").lower(),
            )
            if identity not in seen:
                seen.add(identity)
                chain.append(entry)
    return chain


def _entries(raw: Any) -> list[dict[str, Any]]:
    candidates = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        provider = str(candidate.get("provider") or "").strip()
        model = str(candidate.get("model") or "").strip()
        if not provider or not model:
            continue
        normalized = dict(candidate)
        normalized.update({"provider": provider, "model": model})
        base_url = str(candidate.get("base_url") or "").strip().rstrip("/")
        if base_url:
            normalized["base_url"] = base_url
        result.append(normalized)
    return result


__all__ = ["get_fallback_chain", "resolve_entry_api_key"]
