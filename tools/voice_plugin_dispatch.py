"""Runtime dispatch for plugin-owned text-to-speech and STT providers."""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)


def dispatch_tts_plugin(
    *,
    text: str,
    output_path: str,
    provider: str,
    config: Dict[str, Any],
    builtin_names: FrozenSet[str],
    command_provider_shadows: bool,
    default_format: str,
) -> Optional[str]:
    """Run a plugin TTS provider after native and config-local precedence."""

    if not provider:
        return None
    key = provider.lower().strip()
    if key in builtin_names or command_provider_shadows:
        return None
    try:
        from agent.tts_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        plugin_provider = get_provider(key)
        if plugin_provider is None:
            _ensure_plugins_discovered(force=True)
            plugin_provider = get_provider(key)
    except Exception as exc:
        logger.debug("TTS plugin discovery failed: %s", exc)
        return None
    if plugin_provider is None:
        return None
    try:
        available = plugin_provider.is_available()
    except Exception as exc:
        raise RuntimeError(f"TTS plugin '{key}' availability check failed: {exc}") from exc
    if not available:
        raise RuntimeError(
            f"TTS plugin '{key}' is unavailable; check its credentials and dependencies"
        )

    root_config = config if isinstance(config, dict) else {}
    provider_config = root_config.get(key)
    if not isinstance(provider_config, dict):
        provider_config = {}
    voice = provider_config.get("voice") or provider_config.get("voice_id")
    model = provider_config.get("model") or provider_config.get("model_id")
    speed = provider_config.get("speed", root_config.get("speed"))
    output_format = (
        provider_config.get("output_format", default_format)
    )
    written = plugin_provider.synthesize(
        text,
        output_path,
        voice=voice if isinstance(voice, str) and voice else None,
        model=model if isinstance(model, str) and model else None,
        speed=float(speed) if isinstance(speed, (int, float)) else None,
        format=str(output_format).lower() if output_format else "mp3",
    )
    return written if isinstance(written, str) and written else output_path


def tts_plugin_is_voice_compatible(
    provider: str,
    builtin_names: FrozenSet[str],
) -> bool:
    """Return the plugin provider's explicit voice-delivery capability."""

    if not provider or provider.lower().strip() in builtin_names:
        return False
    try:
        from agent.tts_registry import get_provider

        plugin_provider = get_provider(provider.lower().strip())
        return bool(plugin_provider and plugin_provider.voice_compatible)
    except Exception:
        return False


def dispatch_transcription_plugin(
    *,
    file_path: str,
    provider: str,
    builtin_names: FrozenSet[str],
    model: Optional[str] = None,
    language: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Run a plugin STT provider or return ``None`` when no plugin owns it."""

    if not provider:
        return None
    key = provider.lower().strip()
    if key in builtin_names or key == "none":
        return None
    try:
        from agent.transcription_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        plugin_provider = get_provider(key)
        if plugin_provider is None:
            _ensure_plugins_discovered(force=True)
            plugin_provider = get_provider(key)
    except Exception as exc:
        logger.debug("STT plugin discovery failed: %s", exc)
        return None
    if plugin_provider is None:
        return None

    try:
        available = plugin_provider.is_available()
    except Exception as exc:
        logger.warning("STT plugin '%s' availability check failed: %s", key, exc)
        available = False
    if not available:
        return {
            "success": False,
            "transcript": "",
            "error": (
                f"STT plugin '{key}' is unavailable; check its credentials "
                "and dependencies"
            ),
            "provider": key,
        }
    try:
        result = plugin_provider.transcribe(
            file_path,
            model=model,
            language=language,
        )
    except Exception as exc:
        logger.warning("STT plugin '%s' raised: %s", key, exc, exc_info=True)
        return {
            "success": False,
            "transcript": "",
            "error": f"STT plugin '{key}' raised: {exc}",
            "provider": key,
        }
    if not isinstance(result, dict):
        return {
            "success": False,
            "transcript": "",
            "error": f"STT plugin '{key}' returned a non-dict result",
            "provider": key,
        }
    result.setdefault("provider", key)
    return result
