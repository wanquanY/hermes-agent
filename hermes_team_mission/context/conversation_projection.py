from __future__ import annotations

from typing import Any

from hermes_team_mission.context.artifact_refs import dedupe_artifact_refs as _dedupe_artifact_refs
from hermes_team_mission.domain.identities import canonical_node_id
from hermes_team_mission.domain.utils import text


def _conversation_graph_node_id(mission_id: str, node_id: str) -> str:
    return canonical_node_id(mission_id, node_id)


def dedupe_artifact_refs(artifacts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return _dedupe_artifact_refs(artifacts)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    if content is None:
        return ""
    return text(content)


def _team_ref_from_message(message: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    team_ref = metadata.get("team_mission") or metadata.get("teamMission")
    return team_ref if isinstance(team_ref, dict) else {}


def final_deliverable_from_message(message: dict[str, Any] | None) -> dict[str, Any]:
    message = message if isinstance(message, dict) else {}
    if text(message.get("role")) != "assistant":
        return {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    team_ref = _team_ref_from_message(message)
    if text(team_ref.get("kind")) != "final_deliverable":
        return {}
    mission_id = text(team_ref.get("mission_id") or team_ref.get("missionId"))
    if not mission_id:
        return {}
    raw_node_id = text(team_ref.get("node_id") or team_ref.get("nodeId"))
    node_id = raw_node_id
    if raw_node_id and not (
        raw_node_id.startswith(f"{mission_id}:")
        or raw_node_id.startswith(f"team-mission:{mission_id}:")
    ):
        node_id = _conversation_graph_node_id(mission_id, raw_node_id)
    message_id = text(message.get("id"))
    timestamp = message.get("timestamp") or 0
    task_id = text(team_ref.get("task_id") or team_ref.get("taskId"))
    source_run_id = text(team_ref.get("source_run_id") or team_ref.get("sourceRunId"))
    source_session_id = text(team_ref.get("source_session_id") or team_ref.get("sourceSessionId"))
    projection = {
        "message_id": message_id,
        "messageId": message_id,
        "mission_id": mission_id,
        "missionId": mission_id,
        "task_id": task_id,
        "taskId": task_id,
        "node_id": node_id,
        "nodeId": node_id,
        "hermes_node_id": raw_node_id,
        "hermesNodeId": raw_node_id,
        "source_run_id": source_run_id,
        "sourceRunId": source_run_id,
        "source_session_id": source_session_id,
        "sourceSessionId": source_session_id,
        "source_seq": text(team_ref.get("source_seq") or team_ref.get("sourceSeq")),
        "sourceSeq": text(team_ref.get("source_seq") or team_ref.get("sourceSeq")),
        "content": _message_content_text(message.get("content")),
        "text": _message_content_text(message.get("content")),
        "created_at": timestamp,
        "createdAt": timestamp,
    }
    artifact_refs = dedupe_artifact_refs([
        *list(team_ref.get("artifact_refs") or []),
        *list(team_ref.get("artifactRefs") or []),
        *list(metadata.get("artifact_refs") or []),
        *list(metadata.get("artifactRefs") or []),
    ])
    if artifact_refs:
        projection["artifact_refs"] = artifact_refs
        projection["artifactRefs"] = artifact_refs
    return projection


def final_deliverable_with_artifact_refs(
    deliverable: dict[str, Any],
    artifact_refs_by_mission: dict[str, list[dict[str, Any]]],
    artifact_refs_by_task: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    deliverable = dict(deliverable)
    mission_id = text(deliverable.get("mission_id") or deliverable.get("missionId"))
    task_id = text(deliverable.get("task_id") or deliverable.get("taskId"))
    artifact_refs = dedupe_artifact_refs([
        *list(deliverable.get("artifact_refs") or []),
        *list(deliverable.get("artifactRefs") or []),
        *list(artifact_refs_by_mission.get(mission_id) or []),
        *list(artifact_refs_by_task.get((mission_id, task_id)) or []),
    ])
    if artifact_refs:
        deliverable["artifact_refs"] = artifact_refs
        deliverable["artifactRefs"] = artifact_refs
    return deliverable


def final_deliverable_for_frame(
    deliverables_by_mission: dict[str, list[dict[str, Any]]],
    deliverables_by_task: dict[tuple[str, str], list[dict[str, Any]]],
    mission_id: str,
    task_id: str,
    artifact_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    mission_id = text(mission_id)
    task_id = text(task_id)
    candidates = deliverables_by_task.get((mission_id, task_id)) if task_id else None
    if not candidates:
        candidates = deliverables_by_mission.get(mission_id) or []
    if not candidates:
        return {}
    deliverable = dict(candidates[-1])
    artifacts = dedupe_artifact_refs([
        *list(deliverable.get("artifact_refs") or []),
        *list(artifact_refs or []),
    ])
    deliverable["artifact_refs"] = artifacts
    deliverable["artifactRefs"] = artifacts
    return deliverable


def message_with_deliverable_artifact_refs(
    message: dict[str, Any],
    deliverables: list[dict[str, Any]],
) -> dict[str, Any]:
    if not message:
        return {}
    message_id = text(message.get("message_id") or message.get("messageId") or message.get("id"))
    if not message_id:
        return message
    deliverable = next(
        (
            item for item in deliverables
            if text(item.get("message_id") or item.get("messageId")) == message_id
        ),
        {},
    )
    artifact_refs = dedupe_artifact_refs(list(deliverable.get("artifact_refs") or deliverable.get("artifactRefs") or []))
    if not artifact_refs:
        return message
    enriched = dict(message)
    enriched["artifact_refs"] = artifact_refs
    enriched["artifactRefs"] = artifact_refs
    for key in ("team_mission", "teamMission"):
        team_ref = dict(enriched.get(key) or {}) if isinstance(enriched.get(key), dict) else {}
        if team_ref:
            team_ref["artifact_refs"] = artifact_refs
            team_ref["artifactRefs"] = artifact_refs
            enriched[key] = team_ref
    return enriched


def message_summary_from_message(message: dict[str, Any] | None) -> dict[str, Any]:
    message = message if isinstance(message, dict) else {}
    message_id = text(message.get("id"))
    role = text(message.get("role"))
    content = _message_content_text(message.get("content"))
    preview = content.strip()
    timestamp = message.get("timestamp") or 0
    summary = {
        "id": message_id,
        "message_id": message_id,
        "messageId": message_id,
        "role": role,
        "content": content,
        "text": content,
        "preview": preview,
        "timestamp": timestamp,
        "created_at": timestamp,
        "createdAt": timestamp,
    }
    team_ref = _team_ref_from_message(message)
    if team_ref:
        summary["team_mission"] = team_ref
        summary["teamMission"] = team_ref
    return summary
