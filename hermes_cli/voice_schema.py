"""Dynamic dashboard schema options for pluggable voice providers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from hermes_cli.config import cfg_get


def is_command_provider_block(value: Any) -> bool:
    """Return whether *value* declares a command-backed voice provider."""
    if not isinstance(value, dict):
        return False
    provider_type = str(value.get("type") or "").strip().lower()
    if provider_type and provider_type != "command":
        return False
    command = value.get("command")
    return isinstance(command, str) and bool(command.strip())


def _runtime_builtin_names(kind: str) -> frozenset[str]:
    if kind == "tts":
        from tools.tts_tool import BUILTIN_TTS_PROVIDERS

        return BUILTIN_TTS_PROVIDERS
    if kind == "stt":
        from tools.transcription_tools import BUILTIN_STT_PROVIDERS

        return BUILTIN_STT_PROVIDERS
    raise ValueError(f"Unsupported voice provider kind: {kind}")


def _registered_provider_names(kind: str) -> Iterable[str]:
    """Yield plugin registry names after normal cached plugin discovery."""
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        if kind == "tts":
            from agent.tts_registry import list_providers
        else:
            from agent.transcription_registry import list_providers
        for provider in list_providers():
            name = getattr(provider, "name", None)
            if isinstance(name, str):
                yield name
    except Exception:
        return


def provider_options(
    kind: str,
    builtin_display_names: Iterable[str],
    config: Mapping[str, Any],
) -> List[str]:
    """Merge built-ins, command providers, plugins, and the active value.

    Resolution order mirrors runtime ownership: built-ins reserve their names,
    a user command provider wins over a same-name plugin, then plugin providers
    fill the extensibility layer. The active value is retained even when its
    backing provider is temporarily unavailable so the UI never silently
    rewrites a user's configuration.
    """
    names = [str(name) for name in builtin_display_names]
    seen = {name.strip().lower() for name in names}
    runtime_builtins = _runtime_builtin_names(kind)

    def add(name: Any) -> None:
        if not isinstance(name, str):
            return
        display = name.strip()
        normalized = display.lower()
        if display and normalized not in seen:
            names.append(display)
            seen.add(normalized)

    section = config.get(kind)
    if not isinstance(section, dict):
        section = {}

    providers = section.get("providers")
    candidate_blocks = []
    if isinstance(providers, dict):
        candidate_blocks.append(providers)
    candidate_blocks.append({key: value for key, value in section.items() if key != "providers"})

    for block in candidate_blocks:
        for name, value in block.items():
            if (
                isinstance(name, str)
                and name.strip().lower() not in runtime_builtins
                and is_command_provider_block(value)
            ):
                add(name)

    for name in _registered_provider_names(kind):
        add(name)
    add(cfg_get(config, kind, "provider"))
    return names


def schema_with_voice_provider_options(
    schema: Dict[str, Dict[str, Any]],
    config: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Return a non-mutating schema overlay with live voice provider names."""
    overlay: Dict[str, Dict[str, Any]] = {}
    for kind in ("tts", "stt"):
        key = f"{kind}.provider"
        entry = schema.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("options"), list):
            continue
        options = provider_options(kind, entry["options"], config)
        if options != entry["options"]:
            overlay[key] = {**entry, "options": options}
    if not overlay:
        return schema
    fields = dict(schema)
    fields.update(overlay)
    return fields
