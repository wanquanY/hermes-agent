"""Display-only model identity formatting.

Wire IDs remain untouched. This module owns every lossy presentation rule so
status bars, switch confirmations, and gateways cannot accidentally feed a
shortened identifier back into provider routing or persistence.
"""

from __future__ import annotations

from typing import Any

_OPAQUE_MODEL_PREFIXES = ("ri.language-model-service..language-model.",)
_reverse_alias_cache: dict[str, str] | None = None


def format_model_for_display(model_name: str) -> str:
    """Strip known opaque proxy prefixes for presentation only."""
    if not model_name:
        return model_name
    for prefix in _OPAQUE_MODEL_PREFIXES:
        if model_name.startswith(prefix):
            return model_name[len(prefix) :] or model_name
    return model_name


def _prefer_shortest_alias(mapping: dict[str, str], model: str, alias: Any) -> None:
    if not model or not isinstance(alias, str) or not alias.strip():
        return
    candidate = alias.strip()
    current = mapping.get(model)
    if current is None or (len(candidate), candidate) < (len(current), current):
        mapping[model] = candidate


def _build_reverse_aliases() -> dict[str, str]:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    result: dict[str, str] = {}
    aliases = config.get("model_aliases")
    if isinstance(aliases, dict):
        for alias, entry in aliases.items():
            if isinstance(entry, dict):
                model = str(entry.get("model") or "").strip()
                _prefer_shortest_alias(result, model, alias)

    model_config = config.get("model")
    simple_aliases = model_config.get("aliases") if isinstance(model_config, dict) else None
    if isinstance(simple_aliases, dict):
        for alias, value in simple_aliases.items():
            if not isinstance(value, str):
                continue
            declared = value.strip()
            model = declared.split("/", 1)[1] if "/" in declared else declared
            _prefer_shortest_alias(result, model, alias)
    return result


def display_model_name(model_name: str) -> str:
    """Prefer a configured alias, then shorten a known opaque wire ID."""
    global _reverse_alias_cache
    if not model_name:
        return model_name
    if _reverse_alias_cache is None:
        try:
            _reverse_alias_cache = _build_reverse_aliases()
        except Exception:
            _reverse_alias_cache = {}
    alias = _reverse_alias_cache.get(model_name)
    if alias:
        return alias
    leaf = model_name.split("/")[-1]
    return format_model_for_display(leaf)


def reset_model_display_cache() -> None:
    """Clear the config-derived cache after an in-process config change."""
    global _reverse_alias_cache
    _reverse_alias_cache = None
