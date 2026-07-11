"""Pure projection of a shared conversation into one member's perspective."""

from __future__ import annotations

from typing import Any


IDENTITY_CONTRACT_METADATA_KEY = "team_member_identity_contract"


def is_team_member_identity_contract_message(message: dict[str, Any]) -> bool:
    if not isinstance(message, dict):
        return False
    return bool(_message_metadata(message).get(IDENTITY_CONTRACT_METADATA_KEY))


def transform_to_member_perspective(
    messages: list[dict[str, Any]],
    viewing_participant_id: str,
    participants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project shared history into one participant's speaker view."""

    viewing = _text(viewing_participant_id)
    participant_by_id = {
        _text(participant.get("participant_id")): participant
        for participant in (participants or [])
        if isinstance(participant, dict) and _text(participant.get("participant_id"))
    }
    projected: list[dict[str, Any]] = []
    has_identity_contract = False
    drop_following_tools = False
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if is_team_member_identity_contract_message(message):
            has_identity_contract = True

        role = _text(message.get("role"))
        if role == "tool":
            if not drop_following_tools:
                projected.append(message)
            continue

        drop_following_tools = False
        if role == "user" or role != "assistant":
            projected.append(message)
            continue

        metadata = _message_metadata(message)
        speaker_participant_id = _text(
            metadata.get("participant_id") or message.get("participant_id")
        )
        if speaker_participant_id and speaker_participant_id == viewing:
            projected.append(message)
            continue

        content = str(message.get("content") or "")
        if not content:
            drop_following_tools = True
            continue

        speaker_name = (
            _participant_display_name(
                speaker_participant_id,
                participant_by_id,
                message,
                metadata,
            )
            if speaker_participant_id
            else ""
        ) or "未知发言者"
        transformed_metadata = {
            **metadata,
            "transformed_from_role": "assistant",
            "transformed_speaker_pid": speaker_participant_id,
            "transformed_speaker_name": speaker_name,
        }
        transformed = {
            **message,
            "role": "user",
            "content": f"[{speaker_name}] {content}",
            "metadata": transformed_metadata,
        }
        for key in (
            "tool_calls",
            "tool_call_id",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
        ):
            transformed.pop(key, None)
        projected.append(transformed)
        drop_following_tools = True

    if not has_identity_contract:
        projected.insert(0, _identity_contract_message(viewing, participant_by_id))
    return projected


def _identity_contract_message(
    viewing_participant_id: str,
    participant_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    participant = participant_by_id.get(viewing_participant_id) or {}
    display_name = _participant_display_name(
        viewing_participant_id,
        participant_by_id,
        participant,
        participant,
    ) or viewing_participant_id
    role = _participant_role(participant)
    return {
        "role": "system",
        "content": (
            f"你是 {display_name}(角色:{role}),团队会话中的一名成员。\n"
            "历史中带 `[某某]` 前缀、以 user 角色出现的消息,是团队里其他参与者的发言,仅供了解上下文。\n"
            "你绝不能冒充任何其他参与者;你的回复不要以 `[任何名字]` 前缀开头。"
        ),
        "metadata": {
            IDENTITY_CONTRACT_METADATA_KEY: True,
            "participant_id": viewing_participant_id,
            "display_name": display_name,
            "role": role,
        },
    }


def _message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _participant_display_name(
    participant_id: str,
    participant_by_id: dict[str, dict[str, Any]],
    message: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    participant = participant_by_id.get(participant_id) or {}
    for source in (participant, metadata, message):
        for key in ("display_name", "speaker_name", "name", "role"):
            value = _text(source.get(key))
            if value:
                return value
    return ""


def _participant_role(participant: dict[str, Any]) -> str:
    return (
        _text(participant.get("role"))
        or _text(participant.get("participant_role"))
        or _text(participant.get("participantRole"))
        or "未指定"
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "IDENTITY_CONTRACT_METADATA_KEY",
    "is_team_member_identity_contract_message",
    "transform_to_member_perspective",
]
