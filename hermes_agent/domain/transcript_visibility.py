"""Shared visibility contract for Hermes conversation messages.

Hermes uses a small number of synthetic messages to steer retries and resume
background work.  Those messages are part of the runtime protocol, not human
conversation.  This module is the single owner for distinguishing:

* ephemeral runtime scaffolding, which must never be persisted; and
* durable internal context, which may be persisted for model replay but must
  never appear in a public conversation transcript.

The legacy text checks exist only to quarantine rows written before structured
visibility metadata was introduced.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TRANSCRIPT_VISIBILITY_KEY = "transcript_visibility"
TRANSCRIPT_VISIBILITY_INTERNAL = "internal"
TRANSCRIPT_VISIBILITY_PUBLIC = "public"
SYNTHETIC_KIND_KEY = "synthetic_kind"
REPEATED_INTERIM_COMMENTARY_KIND = "repeated_interim_commentary"

EMPTY_RESPONSE_RECOVERY_PROMPT = (
    "You just executed tool calls but returned an empty response. "
    "Please process the tool results above and continue with the task."
)
BACKGROUND_PROCESS_NOTIFICATION_PREFIX = "[IMPORTANT: Background process "

# These messages exist only while a model call is being repaired or continued.
# They can remain in the live provider buffer for the duration of the turn, but
# no durable history or user-facing read model may retain them.
EPHEMERAL_RUNTIME_MESSAGE_FLAGS = frozenset(
    {
        "_empty_recovery_synthetic",
        "_empty_terminal_sentinel",
        "_pre_verify_synthetic",
        "_synthetic_continuation",
        "_thinking_prefill",
        "_verification_stop_synthetic",
    }
)

# Compression can intentionally retain this marker in durable runtime history.
# It therefore is private, but not ephemeral.
DURABLE_INTERNAL_MESSAGE_FLAGS = frozenset({"_todo_snapshot_synthetic"})
PRIVATE_MESSAGE_FLAGS = (
    EPHEMERAL_RUNTIME_MESSAGE_FLAGS | DURABLE_INTERNAL_MESSAGE_FLAGS
)


def internal_transcript_metadata(
    metadata: Mapping[str, Any] | None = None,
    *,
    synthetic_kind: str,
) -> dict[str, Any]:
    """Return metadata marking a durable runtime-only conversation message."""

    result = dict(metadata or {})
    result[TRANSCRIPT_VISIBILITY_KEY] = TRANSCRIPT_VISIBILITY_INTERNAL
    stable_kind = str(synthetic_kind or "").strip()
    if stable_kind:
        result[SYNTHETIC_KIND_KEY] = stable_kind
    return result


def is_ephemeral_runtime_message(message: Any) -> bool:
    """Whether ``message`` is provider scaffolding that must not be durable."""

    if not isinstance(message, Mapping):
        return False
    return any(bool(message.get(flag)) for flag in EPHEMERAL_RUNTIME_MESSAGE_FLAGS)


def is_public_transcript_message(message: Any) -> bool:
    """Whether a message belongs in a human-visible conversation transcript."""

    return private_transcript_reason(message) == ""


def private_transcript_reason(message: Any) -> str:
    """Return the visibility exclusion reason, or ``""`` when public."""

    if not isinstance(message, Mapping):
        return ""

    metadata = _mapping(message.get("metadata"))
    visibility = _text(
        metadata.get(TRANSCRIPT_VISIBILITY_KEY)
        or metadata.get("transcriptVisibility")
        or message.get(TRANSCRIPT_VISIBILITY_KEY)
        or message.get("transcriptVisibility")
    ).lower()
    if visibility == TRANSCRIPT_VISIBILITY_INTERNAL:
        return "structured_internal_visibility"

    for flag in PRIVATE_MESSAGE_FLAGS:
        if bool(message.get(flag)):
            return f"private_flag:{flag}"

    # Quarantine rows created before structured visibility metadata existed.
    # Restrict the signatures to role=user so assistant explanations quoting
    # these strings remain visible.
    if _text(message.get("role")).lower() != "user":
        return ""
    content = _message_text(message.get("content"))
    if content == EMPTY_RESPONSE_RECOVERY_PROMPT:
        return "legacy_empty_response_recovery"
    if content.startswith(BACKGROUND_PROCESS_NOTIFICATION_PREFIX):
        return "legacy_background_process_notification"
    return ""


def stable_transcript_message_id(message: Any) -> str:
    """Return the cross-projection identity used for visible deduplication."""

    if not isinstance(message, Mapping):
        return ""
    metadata = _mapping(message.get("metadata"))
    return _text(
        message.get("conversation_message_id")
        or message.get("conversationMessageId")
        or metadata.get("conversation_message_id")
        or metadata.get("conversationMessageId")
    )


class PublicTranscriptVisibilityPolicy:
    """Base policy applied to every public Hermes transcript."""

    def includes(self, message: dict[str, Any]) -> bool:
        return is_public_transcript_message(message)

    def stable_message_id(self, message: dict[str, Any]) -> str:
        return stable_transcript_message_id(message)


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for part in value:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in {"text", "input_text", "output_text"}:
            parts.append(str(part.get("text") or ""))
    return "\n".join(parts).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "BACKGROUND_PROCESS_NOTIFICATION_PREFIX",
    "DURABLE_INTERNAL_MESSAGE_FLAGS",
    "EMPTY_RESPONSE_RECOVERY_PROMPT",
    "EPHEMERAL_RUNTIME_MESSAGE_FLAGS",
    "PRIVATE_MESSAGE_FLAGS",
    "PublicTranscriptVisibilityPolicy",
    "REPEATED_INTERIM_COMMENTARY_KIND",
    "SYNTHETIC_KIND_KEY",
    "TRANSCRIPT_VISIBILITY_INTERNAL",
    "TRANSCRIPT_VISIBILITY_KEY",
    "TRANSCRIPT_VISIBILITY_PUBLIC",
    "internal_transcript_metadata",
    "is_ephemeral_runtime_message",
    "is_public_transcript_message",
    "private_transcript_reason",
    "stable_transcript_message_id",
]
