from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_agent.repositories.message_content_codec import decode_message_content
from hermes_team_mission.context.conversation_projection import dedupe_artifact_refs
from hermes_team_mission.domain.assignees import assignee_public_fields
from hermes_team_mission.domain.assignees import resolve_node_assignee
from hermes_team_mission.domain.identities import canonical_node_id
from hermes_team_mission.domain.node_kinds import metadata_with_normalized_node_kind
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind


class TeamMissionRowMapper:
    """Maps canonical Team Mission storage rows to public dictionaries."""

    def conversation_from_row(
        self,
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
        display_title_source = _text(
            (metadata if isinstance(metadata, dict) else {}).get("display_title_source")
            or (metadata if isinstance(metadata, dict) else {}).get("displayTitleSource")
            or "conversation_title"
        )
        updated_at = _row_value(row, "activity_updated_at", _row_value(row, "updated_at", 0))
        message_count = int(_row_value(row, "message_count", 0) or 0)
        return {
            "conversation_id": str(_row_value(row, "conversation_id", "") or ""),
            "team_id": str(_row_value(row, "team_id", "") or ""),
            "conversation_session_id": str(_row_value(row, "conversation_session_id", "") or ""),
            "title": str(_row_value(row, "title", "") or ""),
            "display_title": str(_row_value(row, "title", "") or ""),
            "display_title_source": display_title_source,
            "objective": str(_row_value(row, "objective", "") or ""),
            "workspace_id": str(_row_value(row, "workspace_id", "") or ""),
            "workspace_path": str(_row_value(row, "workspace_path", "") or ""),
            "status": str(_row_value(row, "status", "") or ""),
            "active_mission_id": str(_row_value(row, "projected_active_mission_id", "") or ""),
            "created_by_user_id": str(_row_value(row, "created_by_user_id", "") or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": float(_row_value(row, "created_at", 0) or 0),
            "updated_at": float(updated_at or 0),
            "message_count": message_count,
        }

    def mission_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "mission_id": str(row["mission_id"] or ""),
            "conversation_id": str(_row_value(row, "conversation_id", "") or ""),
            "team_id": str(row["team_id"] or ""),
            "title": str(row["title"] or ""),
            "objective": str(row["objective"] or ""),
            "workspace_id": str(row["workspace_id"] or ""),
            "workspace_path": str(row["workspace_path"] or ""),
            "mode": str(row["mode"] or ""),
            "status": str(row["status"] or ""),
            "leader_session_id": str(row["leader_session_id"] or ""),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
            "completed_at": row["completed_at"],
            "metadata": _json_loads(row["metadata_json"], {}),
        }

    def node_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        metadata = _json_loads(row["metadata_json"], {})
        raw_kind = str(row["kind"] or "")
        kind = normalize_team_mission_node_kind(raw_kind)
        metadata = metadata_with_normalized_node_kind(
            metadata,
            raw_kind=raw_kind,
            canonical_kind=kind,
        )
        runtime_conversation_session_id = _text(_row_value(row, "runtime_conversation_session_id", ""))
        execution_session_id = _text(_row_value(row, "execution_session_id", ""))
        # CR-P3.3: graph identity only; for speaker use participant_id.
        return {
            "node_id": str(row["node_id"] or ""),
            "mission_id": str(row["mission_id"] or ""),
            "kind": kind,
            "title": str(row["title"] or ""),
            "objective": str(row["objective"] or ""),
            "status": str(row["status"] or ""),
            "assignee_profile_id": str(row["assignee_profile_id"] or ""),
            "assignee_profile_version_id": str(row["assignee_profile_version_id"] or ""),
            "canonical_node_id": _text(_row_value(row, "canonical_node_id", "")) or canonical_node_id(
                _text(_row_value(row, "mission_id", "")),
                _text(_row_value(row, "node_id", "")),
            ),
            "canonicalNodeId": _text(_row_value(row, "canonical_node_id", "")) or canonical_node_id(
                _text(_row_value(row, "mission_id", "")),
                _text(_row_value(row, "node_id", "")),
            ),
            "task_frame_id": _text(_row_value(row, "task_frame_id", "")) or (
                f"mission-frame:{_text(_row_value(row, 'mission_id', ''))}"
                if _text(_row_value(row, "mission_id", ""))
                else ""
            ),
            "taskFrameId": _text(_row_value(row, "task_frame_id", "")) or (
                f"mission-frame:{_text(_row_value(row, 'mission_id', ''))}"
                if _text(_row_value(row, "mission_id", ""))
                else ""
            ),
            "runtime_conversation_session_id": runtime_conversation_session_id,
            "runtimeConversationSessionId": runtime_conversation_session_id,
            "execution_session_id": execution_session_id,
            "executionSessionId": execution_session_id,
            "runtime_scope_key": str(row["runtime_scope_key"] or ""),
            "runtimeScopeKey": str(row["runtime_scope_key"] or ""),
            "output_contract": _json_loads(row["output_contract_json"], {}),
            "metadata": metadata,
            **assignee_public_fields(metadata),
            "position_x": float(row["position_x"] or 0),
            "position_y": float(row["position_y"] or 0),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def node_with_resolved_assignee(
        self,
        node: dict[str, Any] | None,
        *,
        mission_metadata: dict[str, Any] | None = None,
        leader_node: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(node, dict) or not node:
            return {}
        resolved_profile_id, resolved_profile_version_id, resolved_runtime_scope_key, resolved_metadata = resolve_node_assignee(
            mission_id=str(node.get("mission_id") or ""),
            node_id=str(node.get("node_id") or ""),
            kind=normalize_team_mission_node_kind(node.get("kind")),
            incoming_profile_id=str(node.get("assignee_profile_id") or ""),
            incoming_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
            incoming_runtime_scope_key=str(node.get("runtime_scope_key") or ""),
            metadata=dict(node.get("metadata") or {}),
            mission_metadata=mission_metadata if isinstance(mission_metadata, dict) else {},
            existing_node=node,
            leader_node=leader_node if isinstance(leader_node, dict) else {},
        )
        resolved_node = {
            **node,
            "assignee_profile_id": resolved_profile_id,
            "assignee_profile_version_id": resolved_profile_version_id,
            "runtime_scope_key": resolved_runtime_scope_key,
            "metadata": resolved_metadata,
        }
        resolved_node.update(assignee_public_fields(resolved_metadata))
        return resolved_node

    def nodes_with_resolved_assignees(
        self,
        nodes: list[dict[str, Any]],
        *,
        mission_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        leader_node = next(
            (
                node
                for node in nodes
                if normalize_team_mission_node_kind(node.get("kind")) == "root"
            ),
            {},
        )
        return [
            self.node_with_resolved_assignee(
                node,
                mission_metadata=mission_metadata,
                leader_node=leader_node,
            )
            for node in nodes
        ]

    def edge_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "edge_id": str(row["edge_id"] or ""),
            "mission_id": str(row["mission_id"] or ""),
            "from_node_id": str(row["from_node_id"] or ""),
            "to_node_id": str(row["to_node_id"] or ""),
            "kind": str(row["kind"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": float(row["created_at"] or 0),
        }

    def run_binding_from_row(
        self,
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        execution_session_id = _text(_row_value(row, "execution_session_id", ""))
        return {
            "mission_id": str(row["mission_id"] or ""),
            "node_id": str(row["node_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "session_id": str(row["session_id"] or ""),
            "execution_session_id": execution_session_id,
            "runtime_scope_key": str(row["runtime_scope_key"] or ""),
            "role": str(row["role"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def latest_run_bindings_by_node(
        self,
        bindings: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            node_id = _text(binding.get("node_id"))
            if not node_id:
                continue
            current = latest.get(node_id)
            if current is None:
                latest[node_id] = binding
                continue
            current_key = (
                float(current.get("updated_at") or 0),
                float(current.get("created_at") or 0),
                _text(current.get("run_id")),
            )
            binding_key = (
                float(binding.get("updated_at") or 0),
                float(binding.get("created_at") or 0),
                _text(binding.get("run_id")),
            )
            if binding_key >= current_key:
                latest[node_id] = binding
        return latest

    def node_with_runtime_binding(
        self,
        node: dict[str, Any] | None,
        binding: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(node, dict) or not node:
            return {}
        if not isinstance(binding, dict) or not binding:
            return node
        node_id = _text(node.get("node_id"))
        if node_id and _text(binding.get("node_id")) and node_id != _text(binding.get("node_id")):
            return node

        session_id = _text(node.get("runtime_conversation_session_id")) or _text(binding.get("session_id"))
        execution_session_id = _text(node.get("execution_session_id")) or _text(binding.get("execution_session_id"))
        runtime_scope_key = _text(node.get("runtime_scope_key")) or _text(binding.get("runtime_scope_key"))
        run_id = _text(binding.get("run_id"))
        # CR-P3.3: graph identity only; for speaker use participant_id.
        canonical_id = _text(node.get("canonical_node_id")) or canonical_node_id(
            _text(node.get("mission_id") or binding.get("mission_id")),
            node_id,
        )
        task_frame_id = _text(node.get("task_frame_id")) or (
            f"mission-frame:{_text(node.get('mission_id') or binding.get('mission_id'))}"
            if _text(node.get("mission_id") or binding.get("mission_id"))
            else ""
        )

        return {
            **node,
            "canonical_node_id": canonical_id,
            "canonicalNodeId": canonical_id,
            "task_frame_id": task_frame_id,
            "taskFrameId": task_frame_id,
            "run_id": run_id,
            "runId": run_id,
            "session_id": session_id,
            "sessionId": session_id,
            "conversation_session_id": session_id,
            "conversationSessionId": session_id,
            "actual_conversation_session_id": session_id,
            "actualConversationSessionId": session_id,
            "runtime_conversation_session_id": session_id,
            "runtimeConversationSessionId": session_id,
            "execution_session_id": execution_session_id,
            "executionSessionId": execution_session_id,
            "runtime_scope_key": runtime_scope_key,
            "runtimeScopeKey": runtime_scope_key,
            "runtime_binding": binding,
            "runtimeBinding": binding,
        }

    def nodes_with_runtime_bindings(
        self,
        nodes: list[dict[str, Any]],
        bindings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        latest_by_node = self.latest_run_bindings_by_node(bindings)
        return [
            self.node_with_runtime_binding(
                node,
                latest_by_node.get(_text(node.get("node_id"))),
            )
            for node in nodes
        ]

    def memory_item_from_row(
        self,
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "team_id": str(row["team_id"] or ""),
            "mission_id": str(row["mission_id"] or ""),
            "conversation_session_id": str(row["conversation_session_id"] or ""),
            "task_id": str(row["task_id"] or ""),
            "scope": str(row["scope"] or ""),
            "kind": str(row["kind"] or ""),
            "content": str(row["content"] or ""),
            "structured_payload": _json_loads(row["structured_payload_json"], {}),
            "source_node_ids": _json_loads(row["source_node_ids_json"], []),
            "source_run_ids": _json_loads(row["source_run_ids_json"], []),
            "artifact_refs": _json_loads(row["artifact_refs_json"], []),
            "workspace_refs": _json_loads(row["workspace_refs_json"], []),
            "confidence": float(row["confidence"] or 0),
            "visibility": str(row["visibility"] or ""),
            "status": str(row["status"] or ""),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
            "invalidated_at": row["invalidated_at"],
        }

    def message_from_row(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        message = dict(row)
        if "content" in message:
            message["content"] = decode_message_content(message["content"])
        if message.get("metadata_json"):
            metadata = _json_loads(message.get("metadata_json"), None)
            if isinstance(metadata, dict):
                message["metadata"] = metadata
        return message

    def memory_edge_from_row(
        self,
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "from_memory_id": str(row["from_memory_id"] or ""),
            "to_memory_id": str(row["to_memory_id"] or ""),
            "relation": str(row["relation"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": float(row["created_at"] or 0),
        }

    def deliverable_from_row(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return deliverable_from_row(row)

    def result_from_row(self, row: sqlite3.Row | None) -> dict[str, Any]:
        return mission_result_from_row(row)


def deliverable_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    deliverable_id = _text(_row_value(row, "deliverable_id"))
    mission_id = _text(_row_value(row, "mission_id"))
    node_id = _text(_row_value(row, "node_id"))
    run_id = _text(_row_value(row, "run_id"))
    task_id = _text(_row_value(row, "task_id"))
    payload = _json_loads(_row_value(row, "payload_json", ""), {})
    artifact_refs = dedupe_artifact_refs(
        _records(_json_loads(_row_value(row, "artifact_refs_json", ""), []))
    )
    next_context = _mapping(
        _json_loads(_row_value(row, "next_context_json", ""), {})
    )
    output_contract = _mapping(
        _json_loads(_row_value(row, "output_contract_json", ""), {})
    )
    return {
        "deliverable_id": deliverable_id,
        "deliverableId": deliverable_id,
        "mission_id": mission_id,
        "missionId": mission_id,
        "node_id": node_id,
        "nodeId": node_id,
        "run_id": run_id,
        "runId": run_id,
        "task_id": task_id,
        "taskId": task_id,
        "status": _text(_row_value(row, "status")),
        "result": _text(_row_value(row, "result")),
        "summary": _text(_row_value(row, "summary")),
        "payload": payload if isinstance(payload, dict) else {},
        "artifact_refs": artifact_refs,
        "artifactRefs": artifact_refs,
        "next_context": next_context,
        "nextContext": next_context,
        "output_contract": output_contract,
        "outputContract": output_contract,
        "source": _text(_row_value(row, "source")),
        "confidence": float(_row_value(row, "confidence", 0) or 0),
        "visibility": _text(_row_value(row, "visibility")),
        "created_at": float(_row_value(row, "created_at", 0) or 0),
        "createdAt": float(_row_value(row, "created_at", 0) or 0),
        "updated_at": float(_row_value(row, "updated_at", 0) or 0),
        "updatedAt": float(_row_value(row, "updated_at", 0) or 0),
    }


def mission_result_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    artifact_refs = _json_loads(_row_value(row, "artifact_refs_json", ""), [])
    artifact_refs = artifact_refs if isinstance(artifact_refs, list) else []
    node_results = _json_loads(_row_value(row, "node_results_json", ""), [])
    node_results = node_results if isinstance(node_results, list) else []
    metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    result_id = _text(_row_value(row, "result_id"))
    mission_id = _text(_row_value(row, "mission_id"))
    activity_id = _text(_row_value(row, "activity_id"))
    summary_text = _text(_row_value(row, "summary_text"))
    return {
        "result_id": result_id,
        "resultId": result_id,
        "mission_id": mission_id,
        "missionId": mission_id,
        "activity_id": activity_id,
        "activityId": activity_id,
        "status": _text(_row_value(row, "status")),
        "outcome": _text(_row_value(row, "outcome")),
        "summary_text": summary_text,
        "summaryText": summary_text,
        "node_results": node_results,
        "nodeResults": node_results,
        "artifact_refs": artifact_refs,
        "artifactRefs": artifact_refs,
        "leader_report_run_id": _text(
            _row_value(row, "leader_report_run_id")
        ),
        "leaderReportRunId": _text(_row_value(row, "leader_report_run_id")),
        "leader_report_message_id": _text(
            _row_value(row, "leader_report_message_id")
        ),
        "leaderReportMessageId": _text(
            _row_value(row, "leader_report_message_id")
        ),
        "metadata": metadata,
        "created_at": float(_row_value(row, "created_at", 0) or 0),
        "createdAt": float(_row_value(row, "created_at", 0) or 0),
        "updated_at": float(_row_value(row, "updated_at", 0) or 0),
        "updatedAt": float(_row_value(row, "updated_at", 0) or 0),
    }


def _json_loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


__all__ = [
    "TeamMissionRowMapper",
    "deliverable_from_row",
    "mission_result_from_row",
]
