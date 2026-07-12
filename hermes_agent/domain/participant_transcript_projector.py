"""Provider-facing projection of a shared transcript for one participant."""

from __future__ import annotations

from typing import Any


SPEAKER_ENVELOPE_VERSION = "participant-speaker-v1"


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


def _speaker_envelope(
    *,
    role: str,
    participant_id: str,
    speaker_name: str,
    viewing_participant_id: str,
) -> str:
    ownership = "You" if participant_id and participant_id == viewing_participant_id else speaker_name
    identity = participant_id or "unknown"
    return f"[{role} | {ownership} | {identity}]"


def project_participant_transcript(
    messages: list[dict[str, Any]],
    *,
    viewing_participant_id: str,
    participants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a clean OpenAI-compatible history with deterministic speakers.

    Only the viewing participant's own prior utterances retain the provider
    ``assistant`` role. Every other assistant utterance becomes attributed
    conversational input under the provider ``user`` role. A foreign speaker
    must never be promoted to ``system`` (which would grant their utterance
    instruction priority), nor remain ``assistant`` (which would transfer the
    speaker's persona and commitments to the viewing actor).

    Tool-call sequences belonging to another or unknown actor are removed
    because replaying them as the viewing actor would transfer action ownership.
    """
    viewing = _text(viewing_participant_id)
    participant_by_id = {
        _text(item.get("participant_id")): dict(item)
        for item in participants or []
        if isinstance(item, dict) and _text(item.get("participant_id"))
    }
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
        if (
            metadata.get("speaker_envelope_version") == SPEAKER_ENVELOPE_VERSION
            and _text(metadata.get("speaker_projected_role")) == role
        ):
            projected.append(message)
            continue

        participant_id = _participant_id(message)
        if role == "user" and not participant_id:
            participant_id = "user"
        speaker_name = _speaker_name(participant_id, participant_by_id, message)
        foreign_assistant = role == "assistant" and participant_id != viewing
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            if foreign_assistant:
                drop_following_tools = True
                continue
            projected.append(message)
            continue

        envelope = _speaker_envelope(
            role=role,
            participant_id=participant_id,
            speaker_name=speaker_name,
            viewing_participant_id=viewing,
        )
        if not content.startswith(envelope):
            message["content"] = f"{envelope}\n{content}"
        metadata.update(
            {
                "speaker_envelope_version": SPEAKER_ENVELOPE_VERSION,
                "speaker_participant_id": participant_id,
                "speaker_display_name": speaker_name,
                "speaker_original_role": role,
                "speaker_projected_role": "user" if foreign_assistant else role,
            }
        )
        message["metadata"] = metadata

        if foreign_assistant:
            message["role"] = "user"
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


__all__ = ["SPEAKER_ENVELOPE_VERSION", "project_participant_transcript"]
