"""Shared text length, thread metadata, and media-routing helpers."""

from __future__ import annotations

_AUDIO_EXTS = frozenset({'.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac'})
_TELEGRAM_AUDIO_ATTACHMENT_EXTS = frozenset({'.mp3', '.m4a'})
_TELEGRAM_VOICE_EXTS = frozenset({'.ogg', '.opus'})


def _platform_name(platform) -> str:
    """Normalize a Platform enum / raw string into a lowercase name."""
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def _thread_metadata_for_source(source, reply_to_message_id: str | None = None) -> dict | None:
    """Build platform-aware thread metadata for adapter sends.

    Most platforms route threaded sends with a generic ``thread_id`` metadata
    value. Telegram private-chat topics created through Hermes' DM-topic helper
    are exposed in updates as ``message_thread_id`` plus a reply anchor. Live
    user-message replies route with ``message_thread_id`` + ``reply_to_message_id``;
    synthetic/resumed sends that have no reply anchor fall back to Telegram's
    ``direct_messages_topic_id`` when the Bot API supports it.
    """
    thread_id = getattr(source, "thread_id", None)
    if thread_id is None:
        return None
    metadata = {"thread_id": thread_id}
    if _platform_name(getattr(source, "platform", None)) == "telegram" and getattr(source, "chat_type", None) == "dm":
        metadata["telegram_dm_topic_reply_fallback"] = True
        tid = str(thread_id)
        if tid and tid not in {"", "1"}:
            metadata["direct_messages_topic_id"] = tid
        anchor = reply_to_message_id or getattr(source, "message_id", None)
        if anchor is not None:
            metadata["telegram_reply_to_message_id"] = str(anchor)
    return metadata


def _reply_anchor_for_event(event) -> str | None:
    """Return reply_to id for platforms that need reply semantics.

    Telegram forum/supergroup topics should be routed by topic metadata, not by
    replying to the triggering message. Hermes-created Telegram private-chat
    topic lanes prefer replying to the triggering user message so the answer
    stays attached to the active lane; synthetic/resumed sends fall back to
    ``direct_messages_topic_id`` metadata when no message id is available.
    """
    source = getattr(event, "source", None)
    platform = _platform_name(getattr(source, "platform", None))
    thread_id = getattr(source, "thread_id", None)
    if platform == "telegram" and thread_id and getattr(source, "chat_type", None) == "dm":
        # Reply to the triggering user message. Replying to Telegram's earlier
        # topic seed/anchor can render the bot response outside the active lane.
        return getattr(event, "message_id", None) or getattr(event, "reply_to_message_id", None)
    if platform == "telegram" and thread_id:
        return None
    if platform == "feishu" and thread_id and getattr(event, "reply_to_message_id", None):
        return getattr(event, "reply_to_message_id", None)
    return getattr(event, "message_id", None)


def should_send_media_as_audio(platform, ext: str, is_voice: bool = False) -> bool:
    """Return True when a media file should use the platform's audio sender.

    Other platforms: every recognized audio extension routes through the
    audio sender.

    Telegram: the Bot API only accepts MP3/M4A for sendAudio and
    Opus/OGG for sendVoice. Opus/OGG is only routed as audio when the
    caller flagged ``is_voice=True`` (so we don't turn a regular audio
    attachment into a voice bubble just because the file happens to be
    Opus). Everything else falls through to document delivery by
    returning ``False``.
    """
    normalized_ext = (ext or "").lower()
    if normalized_ext not in _AUDIO_EXTS:
        return False
    if _platform_name(platform) == "telegram":
        if normalized_ext in _TELEGRAM_VOICE_EXTS:
            return is_voice
        return normalized_ext in _TELEGRAM_AUDIO_ATTACHMENT_EXTS
    return True


def utf16_len(s: str) -> int:
    """Count UTF-16 code units in *s*.

    Telegram's message-length limit (4 096) is measured in UTF-16 code units,
    **not** Unicode code-points.  Characters outside the Basic Multilingual
    Plane (emoji like 😀, CJK Extension B, musical symbols, …) are encoded as
    surrogate pairs and therefore consume **two** UTF-16 code units each, even
    though Python's ``len()`` counts them as one.

    Ported from nearai/ironclaw#2304 which discovered the same discrepancy in
    Rust's ``chars().count()``.
    """
    return len(s.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(s: str, limit: int) -> str:
    """Return the longest prefix of *s* whose UTF-16 length ≤ *limit*.

    Unlike a plain ``s[:limit]``, this respects surrogate-pair boundaries so
    we never slice a multi-code-unit character in half.
    """
    if utf16_len(s) <= limit:
        return s
    # Binary search for the longest safe prefix
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(s[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo]


def _custom_unit_to_cp(s: str, budget: int, len_fn) -> int:
    """Return the largest codepoint offset *n* such that ``len_fn(s[:n]) <= budget``.

    Used by :meth:`BasePlatformAdapter.truncate_message` when *len_fn* measures
    length in units different from Python codepoints (e.g. UTF-16 code units).
    Falls back to binary search which is O(log n) calls to *len_fn*.
    """
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo

