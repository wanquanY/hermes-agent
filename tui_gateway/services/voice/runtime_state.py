from __future__ import annotations

import os
from collections.abc import MutableMapping

VOICE_MODE_ENV = "HERMES_VOICE"
VOICE_TTS_ENV = "HERMES_VOICE_TTS"


def _env(environ: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    return os.environ if environ is None else environ


def _enabled(name: str, environ: MutableMapping[str, str] | None = None) -> bool:
    return _env(environ).get(name, "").strip() == "1"


def _set_enabled(
    name: str, enabled: bool, environ: MutableMapping[str, str] | None = None
) -> None:
    _env(environ)[name] = "1" if enabled else "0"


def voice_mode_enabled(environ: MutableMapping[str, str] | None = None) -> bool:
    """Runtime-only voice-mode flag shared by voice RPC and prompt dispatch."""
    return _enabled(VOICE_MODE_ENV, environ)


def set_voice_mode_enabled(
    enabled: bool, environ: MutableMapping[str, str] | None = None
) -> None:
    _set_enabled(VOICE_MODE_ENV, enabled, environ)


def voice_tts_enabled(environ: MutableMapping[str, str] | None = None) -> bool:
    """Whether final agent replies should be spoken back through TTS."""
    return _enabled(VOICE_TTS_ENV, environ)


def set_voice_tts_enabled(
    enabled: bool, environ: MutableMapping[str, str] | None = None
) -> None:
    _set_enabled(VOICE_TTS_ENV, enabled, environ)
