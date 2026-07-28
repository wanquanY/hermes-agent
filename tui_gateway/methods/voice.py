# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.voice import (
    set_voice_mode_enabled,
    set_voice_tts_enabled,
    voice_mode_enabled,
    voice_tts_enabled,
)

_server = bind_server_globals(globals())


# ── Methods: voice ───────────────────────────────────────────────────


_voice_sid_lock = threading.Lock()
if not hasattr(_server, "_voice_event_sid"):
    _server._voice_event_sid = ""


def _voice_emit(event: str, payload: dict | None = None) -> None:
    """Emit a voice event toward the session that most recently turned the
    mode on. Voice is process-global (one microphone), so there's only ever
    one sid to target; the TUI handler treats an empty sid as "active
    session". Kept separate from _emit to make the lack of per-call sid
    argument explicit."""
    with _voice_sid_lock:
        sid = _server._voice_event_sid
    _emit(event, sid, payload)


def _voice_mode_enabled() -> bool:
    """Current voice-mode flag (runtime-only, CLI parity).

    cli.py initialises ``_voice_mode = False`` at startup and only flips
    it via ``/voice on``; it never reads a persisted enable bit from
    config.yaml.  We match that: no config lookup, env var only.  This
    avoids the TUI auto-starting in REC the next time the user opens it
    just because they happened to enable voice in a prior session.
    """
    return voice_mode_enabled()


def _voice_tts_enabled() -> bool:
    """Whether agent replies should be spoken back via TTS (runtime only)."""
    return voice_tts_enabled()


def _voice_cfg_dict() -> dict:
    """Shape-safe accessor for the ``voice:`` block in config.yaml.

    ``_load_cfg()`` returns raw ``yaml.safe_load()`` output, so both the
    root AND ``voice`` may be any YAML scalar / list / None. A hand-edit
    like ``voice: true`` or a malformed top-level config that parses to
    a scalar would otherwise break ``.get("…")`` and take every
    ``voice.*`` branch down with it (Copilot round-3..7 review on
    #19835). Coerce through ``isinstance`` at every level so malformed
    config falls back to an empty dict instead of crashing /voice.
    """
    cfg = _load_cfg()
    voice_cfg = cfg.get("voice") if isinstance(cfg, dict) else None

    return voice_cfg if isinstance(voice_cfg, dict) else {}


def _voice_record_key() -> str:
    """Current ``voice.record_key`` value, documented default on error."""
    record_key = _voice_cfg_dict().get("record_key")

    return str(record_key) if isinstance(record_key, str) and record_key else "ctrl+b"


@method("voice.toggle")
def _(rid, params: dict) -> dict:
    """CLI parity for the ``/voice`` slash command.

    Subcommands:

    * ``status`` — report mode + TTS flags (default when action is unknown).
    * ``on`` / ``off`` — flip voice *mode* (the umbrella bit). Turning it
      off also tears down any active continuous recording loop. Does NOT
      start recording on its own; recording is driven by ``voice.record``
      (Ctrl+B) after mode is on, matching cli.py's enable/Ctrl+B split.
    * ``tts`` — toggle speech-output of agent replies. Requires mode on
      (mirrors CLI's _toggle_voice_tts guard).
    """
    action = params.get("action", "status")

    if action == "status":
        # Mirror CLI's _show_voice_status: include STT/TTS provider
        # availability so the user can tell at a glance *why* voice mode
        # isn't working ("STT provider: MISSING ..." is the common case).
        # ``record_key`` mirrors the configured ``voice.record_key`` so the
        # TUI can both bind it (frontend ``isVoiceToggleKey``) and display
        # it in /voice status — previously the TUI hardcoded Ctrl+B and
        # ignored the config (#18994).
        payload: dict = {
            "enabled": _voice_mode_enabled(),
            "record_key": _voice_record_key(),
            "tts": _voice_tts_enabled(),
        }
        try:
            from tools.voice_mode import check_voice_requirements

            reqs = check_voice_requirements()
            payload["available"] = bool(reqs.get("available"))
            payload["audio_available"] = bool(reqs.get("audio_available"))
            payload["stt_available"] = bool(reqs.get("stt_available"))
            payload["details"] = reqs.get("details") or ""
        except Exception as e:
            # check_voice_requirements pulls optional transcription deps —
            # swallow so /voice status always returns something useful.
            logger.warning("voice.toggle status: requirements probe failed: %s", e)

        return _ok(rid, payload)

    if action in {"on", "off"}:
        enabled = action == "on"
        # Runtime-only flag (CLI parity) — no _write_config_key, so the
        # next TUI launch starts with voice OFF instead of auto-REC from a
        # persisted stale toggle.
        set_voice_mode_enabled(enabled)

        if not enabled:
            # Disabling the mode must tear the continuous loop down; the
            # loop holds the microphone and would otherwise keep running.
            try:
                from hermes_cli.voice import stop_continuous

                stop_continuous()
            except ImportError:
                pass
            except Exception as e:
                logger.warning("voice: stop_continuous failed during toggle off: %s", e)

        return _ok(
            rid,
            {
                "enabled": enabled,
                "record_key": _voice_record_key(),
                "tts": _voice_tts_enabled(),
            },
        )

    if action == "tts":
        if not _voice_mode_enabled():
            return _err(rid, 4014, "enable voice mode first: /voice on")
        new_value = not _voice_tts_enabled()
        # Runtime-only flag (CLI parity) — see voice.toggle on/off above.
        set_voice_tts_enabled(new_value)
        # Include ``record_key`` on every branch so a /voice tts toggle
        # doesn't reset the TUI's cached shortcut to the default when a
        # user has a custom binding configured (Copilot review, round 2
        # on #19835). Keeps parity with the status/on/off branches above.
        return _ok(
            rid,
            {
                "enabled": True,
                "record_key": _voice_record_key(),
                "tts": new_value,
            },
        )

    return _err(rid, 4013, f"unknown voice action: {action}")


@method("voice.record")
def _(rid, params: dict) -> dict:
    """VAD-bounded push-to-talk capture, CLI-parity.

    ``start`` begins one VAD-bounded capture and emits ``voice.transcript``
    after silence stops the recorder. ``stop`` forces transcription of the
    active buffer, matching classic CLI push-to-talk. The voice wrapper retains
    no-speech counts across single-shot starts, so three consecutive silent
    captures emit ``voice.transcript`` with ``no_speech_limit=True``.
    """
    action = params.get("action", "start")

    if action not in {"start", "stop"}:
        return _err(rid, 4019, f"unknown voice action: {action}")

    try:
        if action == "start":
            if not _voice_mode_enabled():
                return _err(rid, 4015, "voice mode is off — enable with /voice on")

            with _voice_sid_lock:
                _server._voice_event_sid = (
                    params.get("session_id") or _server._voice_event_sid
                )

            from hermes_cli.voice import start_continuous

            # Shape-safe lookups: malformed ``voice:`` YAML (bool/scalar/list)
            # must not crash /voice with a 5025 — fall back to VAD defaults.
            #
            # Exclude ``bool`` from the numeric check since Python's bool is
            # a subclass of int — a hand-edit like ``silence_threshold: true``
            # would otherwise forward as ``1`` instead of falling back to
            # the documented 200 / 3.0 defaults (Copilot round-12 on #19835).
            voice_cfg = _voice_cfg_dict()
            threshold = voice_cfg.get("silence_threshold")
            duration = voice_cfg.get("silence_duration")
            safe_threshold = (
                threshold
                if isinstance(threshold, (int, float))
                and not isinstance(threshold, bool)
                else 200
            )
            safe_duration = (
                duration
                if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                else 3.0
            )
            started = start_continuous(
                on_transcript=lambda t: _voice_emit("voice.transcript", {"text": t}),
                on_status=lambda s: _voice_emit("voice.status", {"state": s}),
                on_silent_limit=lambda: _voice_emit(
                    "voice.transcript", {"no_speech_limit": True}
                ),
                silence_threshold=safe_threshold,
                silence_duration=safe_duration,
                auto_restart=False,
            )
            if started is False:
                return _ok(rid, {"status": "busy"})
            return _ok(rid, {"status": "recording"})

        # action == "stop"
        with _voice_sid_lock:
            _server._voice_event_sid = (
                params.get("session_id") or _server._voice_event_sid
            )

        from hermes_cli.voice import stop_continuous

        stop_continuous(force_transcribe=True)
        return _ok(rid, {"status": "stopped"})
    except ImportError:
        return _err(
            rid, 5025, "voice module not available — install audio dependencies"
        )
    except Exception as e:
        return _err(rid, 5025, str(e))


@method("voice.tts")
def _(rid, params: dict) -> dict:
    text = params.get("text", "")
    if not text:
        return _err(rid, 4020, "text required")
    try:
        from hermes_cli.voice import speak_text

        threading.Thread(target=speak_text, args=(text,), daemon=True).start()
        return _ok(rid, {"status": "speaking"})
    except ImportError:
        return _err(rid, 5026, "voice module not available")
    except Exception as e:
        return _err(rid, 5026, str(e))
