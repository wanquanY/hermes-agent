"""Provider-facing projection of a shared transcript for one participant."""

from __future__ import annotations

from typing import Any

from hermes_agent.domain.participant_message_content import (
    LEGACY_SPEAKER_ENVELOPE_VERSION,
    strip_legacy_speaker_envelopes,
)


PARTICIPANT_PROJECTION_VERSION = "participant-perspective-v3"
_SUPPORTED_PROJECTION_VERSIONS = {
    "participant-structured-v2",
    PARTICIPANT_PROJECTION_VERSION,
}
# Backward-compatible import for callers that still identify the retired v1
# format.  New projection metadata uses ``PARTICIPANT_PROJECTION_VERSION``.
SPEAKER_ENVELOPE_VERSION = LEGACY_SPEAKER_ENVELOPE_VERSION


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(message: dict[str, Any]) -> dict[str, Any]:
    value = message.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _participant_id(message: dict[str, Any]) -> str:
    metadata = _metadata(message)
    return _text(
        message.get("participant_id")
        or metadata.get("participant_id")
        or metadata.get("participantId")
        or metadata.get("speaker_participant_id")
    )


def _speaker_name(
    participant_id: str,
    participants: dict[str, dict[str, Any]],
    message: dict[str, Any],
) -> str:
    participant = participants.get(participant_id) or {}
    metadata = _metadata(message)
    for source in (participant, metadata, message):
        keys = ("display_name", "displayName", "speaker_name", "name")
        if source is participant:
            keys = (*keys, "role")
        for key in keys:
            value = _text(source.get(key))
            if value:
                return value
    if participant_id == "user" or participant_id.startswith("user:"):
        return "User"
    return participant_id or "Unknown Participant"


def _foreign_speaker_envelope(
    *, original_role: str, participant_id: str, speaker_name: str
) -> str:
    """Encode foreign ownership in transient provider-facing content only."""
    return (
        f"[{original_role} | {speaker_name or 'Unknown Participant'} | "
        f"{participant_id or 'unknown'}]"
    )


def _original_role(role: str, metadata: dict[str, Any]) -> str:
    projected_version = _text(metadata.get("speaker_projection_version"))
    legacy_version = _text(metadata.get("speaker_envelope_version"))
    candidate = _text(metadata.get("speaker_original_role"))
    if (
        candidate in {"user", "assistant"}
        and projected_version in _SUPPORTED_PROJECTION_VERSIONS
    ) or (
        candidate in {"user", "assistant"}
        and legacy_version == LEGACY_SPEAKER_ENVELOPE_VERSION
    ):
        return candidate
    return role


def project_participant_transcript(
    messages: list[dict[str, Any]],
    *,
    viewing_participant_id: str,
    participants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a clean OpenAI-compatible history for one participant.

    Only the viewing participant's own prior utterances retain the provider
    ``assistant`` role and keeps its canonical body without an identity prefix.
    A real user's message likewise remains a plain ``user`` message. Every
    foreign assistant utterance becomes conversational input under the
    provider ``user`` role with a transient speaker envelope in its content.

    The envelope exists only in this participant-specific provider view. It is
    never persisted into the canonical transcript. We deliberately do not use
    the provider ``name`` field for foreign assistants: once their role is
    projected to ``user``, a user name makes them indistinguishable from the
    real human user and causes the model to transfer Leader/member identity to
    the human speaker.

    A foreign speaker must never be promoted to ``system`` (which would grant
    their utterance instruction priority), nor remain ``assistant`` (which
    would transfer the speaker's persona and commitments to the viewing actor).

    Tool-call sequences belonging to another or unknown actor are removed
    because replaying them as the viewing actor would transfer action ownership.
    """
    viewing = _text(viewing_participant_id)
    participant_by_id = {
        _text(item.get("participant_id")): dict(item)
        for item in participants or []
        if isinstance(item, dict) and _text(item.get("participant_id"))
    }
    known_participant_ids = set(participant_by_id)
    projected: list[dict[str, Any]] = []
    drop_following_tools = False
    for raw in messages or []:
        if not isinstance(raw, dict):
            continue
        message = dict(raw)
        role = _text(message.get("role"))
        if role == "tool":
            if not drop_following_tools:
                projected.append(message)
            continue
        drop_following_tools = False
        # The actor-scoped system prompt is assembled independently from the
        # shared transcript. Persisted or legacy system rows must never become
        # a second instruction authority for another participant.
        if role == "system":
            continue
        if role not in {"user", "assistant"}:
            projected.append(message)
            continue

        metadata = _metadata(message)
        original_role = _original_role(role, metadata)
        participant_id = _participant_id(message)
        if original_role == "user" and not participant_id:
            participant_id = "user"
        speaker_name = _speaker_name(participant_id, participant_by_id, message)
        foreign_assistant = original_role == "assistant" and participant_id != viewing
        own_assistant = original_role == "assistant" and participant_id == viewing
        content = message.get("content")
        if isinstance(content, str):
            content, _ = strip_legacy_speaker_envelopes(
                content,
                participant_id=participant_id,
                known_participant_ids=known_participant_ids,
                trusted_legacy_projection=(
                    _text(metadata.get("speaker_envelope_version"))
                    == LEGACY_SPEAKER_ENVELOPE_VERSION
                ),
            )
            message["content"] = content
        if not isinstance(content, str) or not content.strip():
            if foreign_assistant:
                drop_following_tools = True
                continue
            projected.append(message)
            continue

        projected_role = (
            "assistant" if own_assistant else "user" if foreign_assistant else role
        )
        metadata.pop("speaker_envelope_version", None)
        metadata.update({
            "speaker_projection_version": PARTICIPANT_PROJECTION_VERSION,
            "speaker_participant_id": participant_id,
            "speaker_display_name": speaker_name,
            "speaker_original_role": original_role,
            "speaker_projected_role": projected_role,
        })
        message["metadata"] = metadata
        message["role"] = projected_role
        message.pop("name", None)

        if foreign_assistant:
            message["content"] = (
                f"{_foreign_speaker_envelope(original_role=original_role, participant_id=participant_id, speaker_name=speaker_name)}\n"
                f"{content}"
            )
            for key in (
                "tool_calls",
                "tool_call_id",
                "reasoning",
                "reasoning_content",
                "reasoning_details",
            ):
                message.pop(key, None)
            drop_following_tools = True
        projected.append(message)
    return projected


__all__ = [
    "PARTICIPANT_PROJECTION_VERSION",
    "SPEAKER_ENVELOPE_VERSION",
    "project_participant_transcript",
]
