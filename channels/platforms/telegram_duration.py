"""Best-effort duration probing for Telegram voice and audio delivery."""

from __future__ import annotations

import os
from typing import Any, Optional


def coerce_duration_seconds(value: Any) -> Optional[int]:
    """Round a raw length to whole positive seconds, or ``None`` if invalid."""
    try:
        seconds = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def probe_voice_duration_seconds(path: str) -> Optional[int]:
    """Read audio length without making delivery depend on optional tooling."""
    if os.path.splitext(path)[1].lower() == ".wav":
        try:
            import wave

            with wave.open(path, "rb") as audio:
                rate = audio.getframerate() or 0
                if rate:
                    duration = coerce_duration_seconds(
                        audio.getnframes() / float(rate)
                    )
                    if duration is not None:
                        return duration
        except Exception:
            pass

    try:
        import mutagen

        audio = mutagen.File(path)
        duration = coerce_duration_seconds(
            getattr(getattr(audio, "info", None), "length", None)
        )
        if duration is not None:
            return duration
    except Exception:
        pass

    try:
        import shutil
        import subprocess

        if shutil.which("ffprobe"):
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if probe.returncode == 0:
                return coerce_duration_seconds(probe.stdout.strip())
    except Exception:
        pass

    return None


# Preserve upstream names at the modular boundary.
_coerce_duration_seconds = coerce_duration_seconds
_probe_voice_duration_seconds = probe_voice_duration_seconds
