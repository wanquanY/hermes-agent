"""Prompt composition and hidden rewrite helpers for expressive TTS backends.

Transport-specific provider code belongs in :mod:`tools.tts_tool`.  This
module owns the orthogonal prompt policy: persona-file resolution, Gemini
audio-tag eligibility, auxiliary-model rewriting, and safe fallbacks.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_AUDIO_TAGS = False
GEMINI_AUDIO_TAG_REWRITE_TASK = "tts_audio_tags"


def config_bool(value: Any, default: bool = False) -> bool:
    """Coerce common config bool spellings without accepting arbitrary text."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def resolve_gemini_persona_prompt_path(
    gemini_config: Mapping[str, Any],
) -> Optional[Path]:
    """Resolve a configured persona file relative to the active Hermes home."""
    raw = gemini_config.get("persona_prompt_file")
    if not isinstance(raw, str) or not raw.strip():
        return None

    path = Path(os.path.expandvars(raw.strip())).expanduser()
    if path.is_absolute():
        return path

    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / path
    except Exception:
        return Path.cwd() / path


def read_gemini_persona_prompt(gemini_config: Mapping[str, Any]) -> str:
    """Read an optional persona file, failing soft on invalid user config."""
    path = resolve_gemini_persona_prompt_path(gemini_config)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Gemini TTS persona prompt file unavailable at %s: %s", path, exc)
        return ""


def compose_gemini_tts_prompt(
    text: str,
    gemini_config: Mapping[str, Any],
    *,
    persona_prompt: Optional[str] = None,
) -> str:
    """Compose performance direction and transcript without speaking metadata."""
    transcript = text.strip()
    if persona_prompt is None:
        persona_prompt = read_gemini_persona_prompt(gemini_config)
    if not persona_prompt:
        return transcript

    preamble = (
        "Synthesize speech from the TRANSCRIPT only. Treat AUDIO PROFILE, "
        "SCENE, DIRECTOR'S NOTES, and SAMPLE CONTEXT as performance direction; "
        "do not speak those sections aloud."
    )
    prompt = persona_prompt
    for pattern in (
        re.compile(r"\{\{\s*transcript\s*\}\}", flags=re.IGNORECASE),
        re.compile(r"\{\s*transcript\s*\}", flags=re.IGNORECASE),
    ):
        if pattern.search(prompt):
            prompt = pattern.sub(transcript, prompt)
            return f"{preamble}\n\n{prompt}".strip()

    return f"{preamble}\n\n{persona_prompt}\n\n#### TRANSCRIPT\n{transcript}".strip()


def gemini_model_supports_audio_tags(model: str) -> bool:
    """Return whether a known Gemini expressive-TTS model accepts audio tags."""
    normalized = (model or "").strip().lower().rsplit("/", 1)[-1]
    return "gemini-3.1" in normalized and "tts" in normalized


def gemini_audio_tags_enabled(gemini_config: Mapping[str, Any], model: str) -> bool:
    """Resolve audio-tag opt-in and reject models without the capability."""
    raw = gemini_config.get("audio_tags")
    if isinstance(raw, Mapping):
        raw = raw.get("enabled")
    if not config_bool(raw, default=DEFAULT_GEMINI_AUDIO_TAGS):
        return False
    if gemini_model_supports_audio_tags(model):
        return True

    logger.warning(
        "Gemini TTS audio_tags enabled, but model %s is not known to support "
        "Gemini audio tags; skipping hidden tag rewrite",
        model,
    )
    return False


def _extract_auxiliary_message_content(response: Any) -> str:
    try:
        choice = response.choices[0]
        message = getattr(choice, "message", None)
        if isinstance(message, Mapping):
            return str(message.get("content") or "")
        return str(getattr(message, "content", "") or "")
    except Exception:
        return ""


def _clean_audio_tag_rewrite(content: str) -> str:
    clean = (content or "").strip()
    fence = re.fullmatch(r"```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```", clean, flags=re.DOTALL)
    return fence.group(1).strip() if fence else clean


def rewrite_gemini_tts_audio_tags(text: str, *, persona_prompt: str = "") -> str:
    """Use the configured auxiliary model to add hidden Gemini audio tags."""
    transcript = text.strip()
    if not transcript:
        return text

    system_prompt = (
        "You rewrite transcripts for Gemini 3.1 Flash TTS by inserting expressive "
        "audio tags.\n\n"
        "Audio tags are inline square-bracket modifiers such as [whispers], "
        "[excitedly], [very slow], [sarcastically], [laughs], [sighs], or [gasp]. "
        "There is no fixed allowlist. Use creative freeform tags naturally to "
        "control tone, pace, emotion, emphasis, and non-verbal sounds. Use English "
        "audio tags even when the spoken transcript is not English.\n\n"
        "Rules:\n"
        "- Preserve the spoken words, order, and meaning.\n"
        "- Do not add new spoken sentences or remove existing spoken words.\n"
        "- Use square brackets for every audio tag.\n"
        "- Do not use SSML or XML tags.\n"
        "- Do not explain or comment.\n"
        "- Return only the tagged TTS script."
    )
    context = persona_prompt.strip() or "(none)"
    try:
        from agent.auxiliary_client import call_llm

        response = call_llm(
            task=GEMINI_AUDIO_TAG_REWRITE_TASK,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"PERSONA AND DIRECTOR CONTEXT:\n{context}\n\n"
                        f"TRANSCRIPT TO TAG:\n{transcript}"
                    ),
                },
            ],
            temperature=0.7,
        )
        tagged = _clean_audio_tag_rewrite(_extract_auxiliary_message_content(response))
        return tagged or text
    except Exception as exc:
        logger.warning("Gemini TTS audio tag rewrite failed; using untagged text: %s", exc)
        return text
