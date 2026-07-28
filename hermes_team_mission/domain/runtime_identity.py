"""Canonical runtime identity for Team Mission events."""

from __future__ import annotations

import json
from typing import Any

from hermes_team_mission.domain.identities import canonical_node_id
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind


def runtime_event_identity(
    *,
    mission: dict[str, Any] | None,
    node: dict[str, Any] | None,
    binding: dict[str, Any] | None,
) -> dict[str, str]:
    mission = _mapping(mission)
    node = _mapping(node)
    binding = _mapping(binding)
    mission_id = _text(
        mission.get("mission_id")
        or binding.get("mission_id")
        or node.get("mission_id")
    )
    mission_metadata = _mapping(mission.get("metadata"))
    node_id = _text(node.get("node_id") or binding.get("node_id"))
    node_kind = normalize_team_mission_node_kind(node.get("kind"))
    output_contract = _mapping(node.get("output_contract"))
    runtime_conversation_session_id = _text(
        binding.get("session_id")
        or node.get("runtime_conversation_session_id")
        or node.get("conversation_session_id")
        or node.get("actual_conversation_session_id")
    )
    execution_session_id = _text(
        binding.get("execution_session_id") or node.get("execution_session_id")
    )
    runtime_scope_key = _text(
        binding.get("runtime_scope_key") or node.get("runtime_scope_key")
    )
    task_id = (
        _task_id_from_node_and_binding(node, binding)
        or _task_id_from_mission(mission)
        or mission_id
    )
    conversation_id = _conversation_id_from_metadata(
        mission_metadata,
        _text(mission.get("conversation_id")),
    )
    conversation_session_id = _conversation_session_id_from_metadata(
        mission_metadata,
        _text(
            mission.get("leader_session_id")
            or mission.get("team_id")
            or mission_id
        ),
    )
    participant_id = _participant_id_from_runtime_context(
        mission=mission,
        node=node,
        binding=binding,
    )
    canonical_id = canonical_node_id(mission_id, node_id)
    output_contract_format = _text(output_contract.get("format"))
    return {
        "mission_id": mission_id,
        "missionId": mission_id,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        "node_id": node_id,
        "nodeId": node_id,
        "canonical_node_id": canonical_id,
        "canonicalNodeId": canonical_id,
        "node_kind": node_kind,
        "nodeKind": node_kind,
        "participant_id": participant_id,
        "participantId": participant_id,
        "output_contract_format": output_contract_format,
        "outputContractFormat": output_contract_format,
        "runtime_conversation_session_id": runtime_conversation_session_id,
        "runtimeConversationSessionId": runtime_conversation_session_id,
        "execution_session_id": execution_session_id,
        "executionSessionId": execution_session_id,
        "runtime_scope_key": runtime_scope_key,
        "runtimeScopeKey": runtime_scope_key,
        "task_id": task_id,
        "taskId": task_id,
        "task_frame_id": f"mission-frame:{mission_id}" if mission_id else "",
        "taskFrameId": f"mission-frame:{mission_id}" if mission_id else "",
    }


def _participant_id_from_runtime_context(
    *,
    mission: dict[str, Any],
    node: dict[str, Any],
    binding: dict[str, Any],
) -> str:
    binding_metadata = _mapping(binding.get("metadata"))
    mission_metadata = _mapping(mission.get("metadata"))
    run_context = _mapping(binding_metadata.get("run_context"))
    serialized_context = _text(
        binding_metadata.get("run_context_json")
        or binding_metadata.get("runContextJson")
    )
    if not run_context and serialized_context:
        try:
            parsed = json.loads(serialized_context)
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        run_context = _mapping(parsed)
    return _text(
        binding_metadata.get("participant_id")
        or binding_metadata.get("participantId")
        or run_context.get("participant_id")
        or run_context.get("participantId")
        or node_participant_id(node)
        or mission_metadata.get("participant_id")
        or mission_metadata.get("participantId")
    )


def node_participant_id(node: dict[str, Any] | None) -> str:
    node = _mapping(node)
    metadata = _mapping(node.get("metadata"))
    return _text(
        node.get("participant_id")
        or node.get("participantId")
        or metadata.get("participant_id")
        or metadata.get("participantId")
    )


def _task_id_from_node_and_binding(
    node: dict[str, Any],
    binding: dict[str, Any],
) -> str:
    return _task_id_from_metadata(_mapping(node.get("metadata"))) or (
        _task_id_from_metadata(_mapping(binding.get("metadata")))
    )


def _task_id_from_mission(mission: dict[str, Any]) -> str:
    return _task_id_from_metadata(_mapping(mission.get("metadata"))) or _text(
        mission.get("mission_id")
    )


def _task_id_from_metadata(metadata: dict[str, Any]) -> str:
    active_task = _mapping(metadata.get("active_task"))
    return _text(
        metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
        or metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
    )


def _conversation_id_from_metadata(
    metadata: dict[str, Any],
    fallback: str,
) -> str:
    return _text(
        metadata.get("conversation_id")
        or metadata.get("conversationId")
        or metadata.get("team_conversation_id")
        or metadata.get("teamConversationId")
        or fallback
    )


def _conversation_session_id_from_metadata(
    metadata: dict[str, Any],
    fallback: str,
) -> str:
    return _text(
        metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("conversation_team_session_id")
        or metadata.get("conversationTeamSessionId")
        or metadata.get("team_session_id")
        or metadata.get("teamSessionId")
        or fallback
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["node_participant_id", "runtime_event_identity"]
