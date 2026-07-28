"""Shared channel event/result models and small projection helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union


class MessageType(Enum):
    """Types of incoming messages."""
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"  # /command style


class ProcessingOutcome(Enum):
    """Result classification for message-processing lifecycle hooks."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    """
    Incoming message from a platform.
    
    Normalized representation that all adapters produce.
    """
    # Message content
    text: str
    message_type: MessageType = MessageType.TEXT
    
    # Source information
    source: SessionSource = None
    
    # Original platform data
    raw_message: Any = None
    message_id: Optional[str] = None

    # Platform-specific update identifier.  For Telegram this is the
    # ``update_id`` from the PTB Update wrapper; other platforms currently
    # ignore it.  Used by ``/restart`` to record the triggering update so the
    # new gateway can advance the Telegram offset past it and avoid processing
    # the same ``/restart`` twice if PTB's graceful-shutdown ACK times out
    # ("Error while calling `get_updates` one more time to mark all fetched
    # updates" in gateway.log).
    platform_update_id: Optional[int] = None
    
    # Media attachments
    # media_urls: local file paths (for vision tool access)
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    
    # Reply context
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None  # Text of the replied-to message (for context injection)
    reply_to_author_id: Optional[str] = None
    reply_to_author_name: Optional[str] = None
    reply_to_is_own_message: bool = False
    
    # Auto-loaded skill(s) for topic/channel bindings (e.g., Telegram DM Topics,
    # Discord channel_skill_bindings).  A single name or ordered list.
    auto_skill: Optional[str | list[str]] = None

    # Per-channel ephemeral system prompt (e.g. Discord channel_prompts).
    # Applied at API call time and never persisted to transcript history.
    channel_prompt: Optional[str] = None

    # Channel context recovered by history backfill (e.g. messages between
    # bot turns that were missed due to require_mention).  Kept separate
    # from ``text`` so the sender-prefix logic in run.py can operate on the
    # trigger message alone, then prepend this context afterward.
    channel_context: Optional[str] = None
    
    # Internal flag — set for synthetic events (e.g. background process
    # completion notifications) that must bypass user authorization checks.
    internal: bool = False

    # Timestamps
    timestamp: datetime = field(default_factory=datetime.now)
    
    def is_command(self) -> bool:
        """Check if this is a command message (e.g., /new, /reset)."""
        return self.text.startswith("/")
    
    def get_command(self) -> Optional[str]:
        """Extract command name if this is a command message."""
        if not self.is_command():
            return None
        # Split on space and get first word, strip the /
        parts = self.text.split(maxsplit=1)
        raw = parts[0][1:].lower() if parts else None
        if raw and "@" in raw:
            raw = raw.split("@", 1)[0]
        # Reject file paths: valid command names never contain /
        if raw and "/" in raw:
            return None
        return raw
    
    def get_command_args(self) -> str:
        """Get the arguments after a command."""
        if not self.is_command():
            return self.text
        parts = self.text.split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        # iOS auto-corrects -- to — (em dash) and - to – (en dash)
        args = args.replace("\u2014\u2014", "--").replace("\u2014", "--").replace("\u2013", "-")
        return args


_PLAINTEXT_GATEWAY_RESTART_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:please\s+)?restart\s+(?:the\s+)?gateway[.!?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?restart\s+(?:the\s+)?hermes\s+gateway[.!?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?restart\s+hermes[.!?\s]*$", re.IGNORECASE),
)


def coerce_plaintext_gateway_command(event: "MessageEvent") -> None:
    """Rewrite a tiny set of DM plaintext admin phrases into slash commands.

    This keeps high-impact operational phrases like ``restart gateway`` out of
    the LLM/tool path, where they can trigger a self-restart from inside the
    currently running agent and leave the gateway stuck in ``draining`` while it
    waits for that same agent to finish.

    Scope is intentionally narrow: DM text messages only, exact restart-style
    phrases only. Group chats keep natural-language semantics.
    """
    try:
        if event is None or event.message_type != MessageType.TEXT:
            return
        text = (event.text or "").strip()
        if not text or text.startswith("/"):
            return
        source = getattr(event, "source", None)
        if getattr(source, "chat_type", None) != "dm":
            return
        for pattern in _PLAINTEXT_GATEWAY_RESTART_PATTERNS:
            if pattern.match(text):
                event.text = "/restart"
                return
    except Exception:
        return


@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False  # True for transient connection errors — base will retry automatically
    # Authoritative server-requested retry delay (for example Telegram
    # FloodWait). Unified retry policy honors it before local backoff.
    retry_after: Optional[float] = None
    # When the adapter had to split an oversized payload across multiple
    # platform messages (e.g. Telegram edit_message overflow split-and-deliver),
    # ``message_id`` is the LAST visible message id (so subsequent edits target
    # the most recent chunk) and these are the additional message ids that
    # made up the full payload, in send order.  Empty tuple for the common
    # single-message case.
    continuation_message_ids: tuple = ()
    # Machine-readable failure category (set only when ``success`` is False).
    # ``error`` stays the human-readable detail string; ``error_kind`` lets
    # consumers branch deterministically instead of substring-matching the raw
    # provider message.  One of the values in :data:`SEND_ERROR_KINDS` or
    # ``None`` (unset / not classified).  Producers should set this via
    # :func:`classify_send_error`.
    error_kind: Optional[str] = None


# Machine-readable send-failure categories.  Kept platform-neutral so every
# adapter can populate ``SendResult.error_kind`` from the same vocabulary and
# the gateway can decide — once, in one place — whether a failure is worth
# surfacing to the user.
#
#   too_long      content exceeded the platform's per-message size cap; the
#                 adapter typically recovers via continuation/split, so this is
#                 informational rather than a hard failure.
#   bad_format    the platform rejected the message markup/entities (parse
#                 error); a plain-text retry is the actionable fix.
#   forbidden     the bot is blocked, kicked, or lacks permission to post to the
#                 target — the bot CANNOT reach the user, so there is nowhere to
#                 surface a notice.
#   not_found     the target chat/thread/message no longer exists.
#   rate_limited  the platform throttled the send (flood control).
#   transient     a connection-level failure that is safe to retry.
#   unknown       classification did not match any known shape.
SEND_ERROR_KINDS = frozenset(
    {
        "too_long",
        "bad_format",
        "forbidden",
        "not_found",
        "rate_limited",
        "transient",
        "unknown",
    }
)


def classify_send_error(exc: Optional[BaseException], error_text: str = "") -> str:
    """Map a send exception / error string to a :data:`SEND_ERROR_KINDS` value.

    Platform-neutral: matches on the lowercased text of ``exc`` (and/or the
    explicit ``error_text``) against the substrings the major messaging APIs
    use.  Conservative — anything unrecognized returns ``"unknown"`` so callers
    never mistake an unclassified failure for a benign one.
    """
    parts = []
    if error_text:
        parts.append(error_text)
    if exc is not None:
        parts.append(str(exc))
        parts.append(exc.__class__.__name__)
    blob = " ".join(parts).lower()
    if not blob.strip():
        return "unknown"
    if "message_too_long" in blob or "too long" in blob or "message is too long" in blob:
        return "too_long"
    if (
        "can't parse entities" in blob
        or "cant parse entities" in blob
        or "can't find end" in blob
        or "unsupported start tag" in blob
        or ("entity" in blob and "parse" in blob)
        or ("bad request" in blob and "entit" in blob)
    ):
        return "bad_format"
    if (
        "forbidden" in blob
        or "bot was blocked" in blob
        or "blocked by the user" in blob
        or "user is deactivated" in blob
        or "not enough rights" in blob
        or "have no rights" in blob
        or "not a member" in blob
    ):
        return "forbidden"
    if (
        "chat not found" in blob
        or "message to edit not found" in blob
        or "message to reply not found" in blob
        or "thread not found" in blob
        or "topic_deleted" in blob
        or "message_id_invalid" in blob
    ):
        return "not_found"
    if (
        "flood" in blob
        or "too many requests" in blob
        or "retry after" in blob
        or "rate limit" in blob
    ):
        return "rate_limited"
    for pat in _RETRYABLE_ERROR_PATTERNS:
        if pat in blob:
            return "transient"
    if "connecttimeout" in blob:
        return "transient"
    return "unknown"


class EphemeralReply(str):
    """System-notice reply that auto-deletes after a TTL.

    Slash-command handlers in ``gateway/run.py`` can return this wrapper
    instead of a plain string to request that the reply message be deleted
    after ``ttl_seconds`` on platforms that support ``delete_message``.

    Subclassing ``str`` keeps the wrapper transparent to anything that
    treats handler return values as text (existing tests use ``in`` /
    ``startswith`` / equality; the ``_process_message_background`` pipeline
    extracts attachments from the string content).  ``isinstance(r,
    EphemeralReply)`` still distinguishes ephemeral replies from plain
    strings so the send path can schedule deletion.

    Platforms that don't override :meth:`BasePlatformAdapter.delete_message`
    silently ignore the TTL — the message is sent normally and left in
    place.  When ``ttl_seconds`` is ``None``, the pipeline uses the
    configured ``display.ephemeral_system_ttl`` default.  A default of ``0``
    disables auto-deletion globally, preserving prior behavior.
    """

    ttl_seconds: Optional[int]

    def __new__(cls, text: str, ttl_seconds: Optional[int] = None):
        instance = super().__new__(cls, text)
        instance.ttl_seconds = ttl_seconds
        return instance

    @property
    def text(self) -> str:
        """Return the underlying text.

        Provided for call sites that want an explicit string conversion,
        though ``str(reply)`` and using ``reply`` directly where a string
        is expected both work identically.
        """
        return str.__str__(self)


def _merge_caption(existing_text: Optional[str], new_text: str) -> str:
    """Merge a new caption into existing text, avoiding duplicates."""
    if not existing_text:
        return new_text
    existing_captions = [caption.strip() for caption in existing_text.split("\n\n")]
    if new_text.strip() not in existing_captions:
        return f"{existing_text}\n\n{new_text}".strip()
    return existing_text


def _invalidate_pending_stt_cache(event: MessageEvent) -> None:
    """Drop cached transcripts whenever new media joins a pending event."""
    for attribute in (
        "_gateway_pending_stt_text",
        "_gateway_pending_stt_transcripts",
        "_gateway_pending_stt_echo_sent",
    ):
        if hasattr(event, attribute):
            delattr(event, attribute)


def merge_pending_message_event(
    pending_messages: Dict[str, MessageEvent],
    session_key: str,
    event: MessageEvent,
    *,
    merge_text: bool = False,
) -> None:
    """Store or merge a pending event for a session.

    Photo bursts/albums often arrive as multiple near-simultaneous PHOTO
    events. Merge those into the existing queued event so the next turn sees
    the whole burst.

    When ``merge_text`` is enabled, rapid follow-up TEXT events are appended
    instead of replacing the pending turn. This is used for Telegram bursty
    follow-ups so a multi-part user thought is not silently truncated to only
    the last queued fragment.
    """
    existing = pending_messages.get(session_key)
    if existing:
        existing_is_photo = getattr(existing, "message_type", None) == MessageType.PHOTO
        incoming_is_photo = event.message_type == MessageType.PHOTO
        existing_has_media = bool(existing.media_urls)
        incoming_has_media = bool(event.media_urls)

        if existing_is_photo and incoming_is_photo:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.text:
                existing.text = _merge_caption(existing.text, event.text)
            _invalidate_pending_stt_cache(existing)
            return

        if existing_has_media or incoming_has_media:
            if incoming_has_media:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
            if event.text:
                if existing.text:
                    existing.text = _merge_caption(existing.text, event.text)
                else:
                    existing.text = event.text
            if existing_is_photo or incoming_is_photo:
                existing.message_type = MessageType.PHOTO
            elif (
                getattr(existing, "message_type", None) == MessageType.TEXT
                and event.message_type != MessageType.TEXT
            ):
                existing.message_type = event.message_type
            _invalidate_pending_stt_cache(existing)
            return

        if (
            merge_text
            and getattr(existing, "message_type", None) == MessageType.TEXT
            and event.message_type == MessageType.TEXT
        ):
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            return

    pending_messages[session_key] = event


# Error substrings that indicate a transient *connection* failure worth retrying.
# "timeout" / "timed out" / "readtimeout" / "writetimeout" are intentionally
# excluded: a read/write timeout on a non-idempotent call (e.g. send_message)
# means the request may have reached the server — retrying risks duplicate
# delivery.  "connecttimeout" is safe because the connection was never
# established.  Platforms that know a timeout is safe to retry should set
# SendResult.retryable = True explicitly.
_RETRYABLE_ERROR_PATTERNS = (
    "connecterror",
    "connectionerror",
    "connectionreset",
    "connectionrefused",
    "connecttimeout",
    "network",
    "broken pipe",
    "remotedisconnected",
    "eoferror",
)


# Type for message handlers.  Handlers may return a plain string (normal
# reply), an ``EphemeralReply`` to opt the reply into auto-deletion, or
# ``None`` when the response was already delivered (e.g. via streaming).
MessageHandler = Callable[[MessageEvent], Awaitable[Optional[Union[str, "EphemeralReply"]]]]


def resolve_channel_prompt(
    config_extra: dict,
    channel_id: str,
    parent_id: str | None = None,
) -> str | None:
    """Resolve a per-channel ephemeral prompt from platform config.

    Looks up ``channel_prompts`` in the adapter's ``config.extra`` dict.
    Prefers an exact match on *channel_id*; falls back to *parent_id*
    (useful for forum threads / child channels inheriting a parent prompt).

    Returns the prompt string, or None if no match is found.  Blank/whitespace-
    only prompts are treated as absent.
    """
    prompts = config_extra.get("channel_prompts") or {}
    if not isinstance(prompts, dict):
        return None

    for key in (channel_id, parent_id):
        if not key:
            continue
        prompt = prompts.get(key)
        if prompt is None:
            continue
        prompt = str(prompt).strip()
        if prompt:
            return prompt
    return None


def resolve_channel_skills(
    config_extra: dict,
    channel_id: str,
    parent_id: str | None = None,
) -> list[str] | None:
    """Resolve auto-loaded skill(s) for a channel/thread from platform config.

    Looks up ``channel_skill_bindings`` in the adapter's ``config.extra`` dict.

    Config format::

        channel_skill_bindings:
          - id: "C0123"          # Slack channel ID or Discord channel/forum ID
            skills: ["skill-a", "skill-b"]
          - id: "D0ABCDE"
            skill: "solo-skill"  # single string also accepted

    Prefers an exact match on *channel_id*; falls back to *parent_id*
    (useful for forum threads / Slack threads inheriting the parent channel's
    binding).

    Returns a deduplicated list of skill names (order preserved), or None if
    no match is found.
    """
    bindings = config_extra.get("channel_skill_bindings") or []
    if not isinstance(bindings, list) or not bindings:
        return None
    ids_to_check: set[str] = set()
    if channel_id:
        ids_to_check.add(str(channel_id))
    if parent_id:
        ids_to_check.add(str(parent_id))
    if not ids_to_check:
        return None
    for entry in bindings:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id", ""))
        if entry_id in ids_to_check:
            skills = entry.get("skills") or entry.get("skill")
            if isinstance(skills, str):
                s = skills.strip()
                return [s] if s else None
            if isinstance(skills, list) and skills:
                seen: list[str] = []
                for name in skills:
                    if not isinstance(name, str):
                        continue
                    nm = name.strip()
                    if nm and nm not in seen:
                        seen.append(nm)
                return seen or None
    return None
