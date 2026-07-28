"""Gateway media context and lightweight media probing."""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from channels.platforms.base_models import MessageType


def build_media_placeholder(event) -> str:
    """Build a text placeholder for media-only events so they are not dropped."""
    parts = []
    media_urls = getattr(event, "media_urls", None) or []
    media_types = getattr(event, "media_types", None) or []
    for i, url in enumerate(media_urls):
        mtype = media_types[i] if i < len(media_types) else ""
        if mtype.startswith("image/") or getattr(event, "message_type", None) == MessageType.PHOTO:
            parts.append(f"[User sent an image: {url}]")
        elif mtype.startswith("audio/"):
            parts.append(f"[User sent audio: {url}]")
        else:
            parts.append(f"[User sent a file: {url}]")
    return "\n".join(parts)


def build_document_context_note(display_name: str, agent_path: str, mtype: str) -> str:
    """Context note prepended to a user turn when they attach a document."""
    if mtype.startswith("text/"):
        return (
            f"[The user sent a text document: '{display_name}'. "
            f"Its content has been included below. "
            f"The file is also saved at: {agent_path}]"
        )
    return (
        f"[The user sent a document: '{display_name}'. It is saved at: {agent_path}. "
        f"Its text is not inlined here (it's a binary format such as PDF or DOCX). "
        f"To read it, extract the document's text yourself — for example with the "
        f"terminal tool or the ocr-and-documents skill — before answering, instead "
        f"of asking the user to paste the contents.]"
    )


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


async def probe_audio_duration(path: str) -> Optional[str]:
    """Best-effort duration probe. Returns formatted MM:SS / HH:MM:SS, or None."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".wav":
        try:
            def _wav_duration() -> float:
                import wave

                with wave.open(path, "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate() or 1
                    return frames / float(rate)

            secs = await asyncio.to_thread(_wav_duration)
            return format_duration(secs)
        except Exception:
            return None

    if ext in (".ogg", ".opus", ".oga"):
        try:
            def _ogg_duration() -> float:
                from mutagen.oggopus import OggOpus

                return float(OggOpus(path).info.length)

            secs = await asyncio.to_thread(_ogg_duration)
            return format_duration(secs)
        except Exception:
            return None

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            return format_duration(float(stdout.decode().strip()))
    except Exception:
        return None

    return None
