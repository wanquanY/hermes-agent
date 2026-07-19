"""Canonical Conversation transcript helpers used by Team Mission."""

from __future__ import annotations

from typing import Any


def text(value: Any) -> str:
    return str(value or "").strip()


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def conversation_session_id(mission: dict[str, Any] | None) -> str:
    mission = mission if isinstance(mission, dict) else {}
    metadata = mapping(mission.get("metadata"))
    return text(
        metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("conversation_team_session_id")
        or metadata.get("conversationTeamSessionId")
        or metadata.get("team_session_id")
        or metadata.get("teamSessionId")
        or mission.get("leader_session_id")
    )


def _metadata_matches(
    message: dict[str, Any],
    *,
    mission_id: str,
    node_id: str,
    kind: str,
    source_run_id: str,
) -> bool:
    metadata = mapping(message.get("metadata"))
    team = mapping(metadata.get("team_mission"))
    if text(team.get("kind")) != kind:
        return False
    if text(team.get("mission_id")) != mission_id:
        return False
    if source_run_id:
        return text(team.get("source_run_id") or team.get("run_id")) == source_run_id
    if node_id and text(team.get("node_id")) != node_id:
        return False
    return True


def _append_message_once(
    db: Any,
    *,
    session_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any],
    participant_id: str,
) -> bool:
    if not session_id or not content:
        return False
    stable_participant_id = text(participant_id)
    if not stable_participant_id:
        raise ValueError("participant_id is required for transcript messages")
    if not db.sessions.get(session_id):
        db.sessions.create(session_id, source="team_mission", transient=False)
    metadata = dict(metadata)
    metadata["participant_id"] = stable_participant_id
    metadata["participantId"] = stable_participant_id
    mission = mapping(metadata.get("team_mission"))
    if mission:
        mission["participant_id"] = stable_participant_id
        mission["participantId"] = stable_participant_id
        metadata["team_mission"] = mission
    mission_id = text(mission.get("mission_id"))
    node_id = text(mission.get("node_id"))
    kind = text(mission.get("kind"))
    source_run_id = text(mission.get("source_run_id") or mission.get("run_id"))
    for message in db.messages.list(session_id):
        if (
            isinstance(message, dict)
            and text(message.get("role")) == role
            and _metadata_matches(
                message,
                mission_id=mission_id,
                node_id=node_id,
                kind=kind,
                source_run_id=source_run_id,
            )
        ):
            return False
    db.messages.append(
        session_id,
        role,
        content,
        participant_id=stable_participant_id,
        metadata=metadata,
    )
    return True


def append_user_task_message(
    db: Any,
    *,
    mission: dict[str, Any],
    objective: str,
    node_id: str = "",
    task_id: str = "",
) -> bool:
    session_id = conversation_session_id(mission)
    mission_id = text((mission or {}).get("mission_id"))
    return _append_message_once(
        db,
        session_id=session_id,
        role="user",
        content=text(objective),
        participant_id="user",
        metadata={
            "team_mission": {
                "kind": "user_task",
                "mission_id": mission_id,
                "node_id": text(node_id),
                "task_id": text(task_id),
                "conversation_session_id": session_id,
            }
        },
    )


__all__ = ["append_user_task_message", "conversation_session_id"]
