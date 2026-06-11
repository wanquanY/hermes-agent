from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from hermes_team_mission_memory_utils import MEMORY_COMMITTED_STATUS as _MEMORY_COMMITTED_STATUS
from hermes_team_mission_memory_utils import stable_id as _stable_id
from hermes_team_mission_memory_utils import text as _text
from hermes_team_mission_conversation_utils import mirror_event_to_conversation as _mirror_team_mission_event
from hermes_team_mission_conversation_state import delete_team_mission_conversation as _delete_team_mission_conversation
from hermes_team_mission_conversation_state import rename_team_mission_conversation as _rename_team_mission_conversation
import hermes_team_mission_memory_state as _memory_state
import hermes_team_mission_graph_state as _graph_state
from hermes_team_mission_assignees import assignee_public_fields as _assignee_public_fields
from hermes_team_mission_assignees import mission_metadata_with_members as _mission_metadata_with_members
from hermes_team_mission_assignees import resolve_node_assignee as _resolve_node_assignee
from hermes_team_mission_node_kinds import TEAM_MISSION_CONTROL_NODE_KINDS
from hermes_team_mission_node_kinds import metadata_with_normalized_node_kind as _metadata_with_normalized_node_kind
from hermes_team_mission_node_kinds import normalize_team_mission_node_kind as _normalize_node_kind
from hermes_team_mission_modes import TeamMissionEdgeSpec
from hermes_team_mission_modes import TeamMissionNodeSpec
from hermes_team_mission_modes import TeamMissionStrategyActions
from hermes_team_mission_modes import strategy_for_mode


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
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
    except Exception:
        return default


_DEPENDENCY_EDGE_KINDS = {"depends_on", "dependency", "blocks", "delegates"}
_DEPENDENCY_SATISFIED_STATUSES = {"completed", "verified"}
_STARTABLE_NODE_STATUSES = {"ready"}
_WAITING_DEPENDENCY_STATUSES = {"todo", "waiting_dependency", "blocked_waiting_dependency"}
_ACTIVE_NODE_STATUSES = {"running", "starting", "waiting_approval"}
_CANCELLABLE_NODE_STATUSES = {
    "todo",
    "ready",
    "waiting_dependency",
    "blocked_waiting_dependency",
    "waiting_approval",
    "starting",
    "running",
    "blocked",
}
_TERMINAL_NODE_STATUSES = {"completed", "verified", "failed", "cancelled", "canceled", "interrupted"}
_ACTIVE_RUN_STATUSES = {
    "queued",
    "starting",
    "running",
    "waiting_approval",
    "cancelling",
    "finalizing",
}
_TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
_EXECUTION_MODES_REQUIRE_FINALIZERS = {"supervised_mission", "autonomous_mission", "manual_graph"}
_NON_WORK_NODE_KINDS = TEAM_MISSION_CONTROL_NODE_KINDS
_TEAM_MISSION_EVENT_SEQ_FACTOR = 1_000_000_000


def _conversation_id_from_metadata(metadata: Dict[str, Any] | None, fallback: str = "") -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    return _text(
        metadata.get("conversation_id")
        or metadata.get("conversationId")
        or metadata.get("team_conversation_id")
        or metadata.get("teamConversationId")
        or fallback
    )


def _stable_session_id_from_metadata(metadata: Dict[str, Any] | None, fallback: str = "") -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    return _text(
        metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("stable_session_id")
        or metadata.get("stableSessionId")
        or metadata.get("stable_team_session_id")
        or metadata.get("stableTeamSessionId")
        or metadata.get("team_session_id")
        or metadata.get("teamSessionId")
        or fallback
    )


def _conversation_status(value: str | None) -> str:
    normalized = _text(value).lower()
    if normalized in {"archived", "deleted"}:
        return normalized
    return "active"


def _node_spec_from_graph_node(node: Dict[str, Any]) -> TeamMissionNodeSpec:
    return TeamMissionNodeSpec(
        node_id=str(node.get("node_id") or ""),
        kind=_normalize_node_kind(node.get("kind")),
        title=str(node.get("title") or ""),
        objective=str(node.get("objective") or ""),
        status=str(node.get("status") or "todo"),
        assignee_profile_id=str(node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=str(node.get("runtime_scope_key") or ""),
        output_contract=dict(node.get("output_contract") or {}),
        metadata=dict(node.get("metadata") or {}),
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )


def _edge_spec_from_graph_edge(edge: Dict[str, Any]) -> TeamMissionEdgeSpec:
    return TeamMissionEdgeSpec(
        edge_id=str(edge.get("edge_id") or ""),
        from_node_id=str(edge.get("from_node_id") or ""),
        to_node_id=str(edge.get("to_node_id") or ""),
        kind=str(edge.get("kind") or "depends_on"),
        metadata=dict(edge.get("metadata") or {}),
    )


def _task_id_from_metadata(metadata: Dict[str, Any] | None) -> str:
    if not isinstance(metadata, dict):
        return ""
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), dict) else {}
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


def _task_id_from_node_and_binding(node: Dict[str, Any] | None, binding: Dict[str, Any] | None = None) -> str:
    node_metadata = node.get("metadata") if isinstance(node, dict) else {}
    binding_metadata = binding.get("metadata") if isinstance(binding, dict) else {}
    return _task_id_from_metadata(node_metadata) or _task_id_from_metadata(binding_metadata)


def _node_matches_task(node: Dict[str, Any] | None, task_id: str) -> bool:
    normalized = _text(task_id)
    if not normalized:
        return True
    return _task_id_from_node_and_binding(node) == normalized


def _edge_matches_task(edge: Dict[str, Any] | None, selected_node_ids: set[str], task_id: str) -> bool:
    if not isinstance(edge, dict):
        return False
    normalized = _text(task_id)
    if not normalized:
        return True
    edge_metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    edge_task_id = _task_id_from_metadata(edge_metadata)
    if edge_task_id:
        return edge_task_id == normalized
    return (
        _text(edge.get("from_node_id")) in selected_node_ids
        and _text(edge.get("to_node_id")) in selected_node_ids
    )


def _task_scoped_node_id(mission_id: str, task_id: str, suffix: str) -> str:
    normalized = _text(task_id)
    if not normalized:
        return f"team-mission:{mission_id}:{suffix}"
    return f"team-mission:{mission_id}:{normalized}:{suffix}"


def _metadata_with_task_id(metadata: Dict[str, Any] | None, task_id: str) -> Dict[str, Any]:
    result = dict(metadata or {})
    normalized = _text(task_id)
    if normalized:
        result.setdefault("task_id", normalized)
    return result


def _event_with_task_id(event: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    normalized = _text(task_id)
    if not normalized or not isinstance(event, dict):
        return event
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        **event,
        "payload": {
            **payload,
            "task_id": payload.get("task_id") or payload.get("taskId") or normalized,
        },
    }


def _strategy_actions_with_task_id(actions: TeamMissionStrategyActions, task_id: str) -> TeamMissionStrategyActions:
    normalized = _text(task_id)
    if not normalized:
        return actions
    return replace(
        actions,
        nodes=tuple(
            replace(node, metadata=_metadata_with_task_id(node.metadata, normalized))
            for node in actions.nodes
        ),
        edges=tuple(
            replace(edge, metadata=_metadata_with_task_id(edge.metadata, normalized))
            for edge in actions.edges
        ),
        approval_requests=tuple(
            {
                **request,
                "task_id": request.get("task_id") or request.get("taskId") or normalized,
            }
            for request in actions.approval_requests
            if isinstance(request, dict)
        ),
        events=tuple(
            _event_with_task_id(event, normalized)
            for event in actions.events
            if isinstance(event, dict)
        ),
    )


class SessionDBTeamMissionMixin:
    """Native Hermes Team Mission graph and run-binding persistence.

    Team Mission stores only mission-specific graph and ownership metadata here.
    Runtime facts remain in ordinary Hermes ``runs``, ``run_events``, and
    ``messages`` tables so stream coalescing, retention, and history semantics
    stay identical to ordinary sessions.
    """

    def _team_mission_conversation_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
        updated_at = _row_value(row, "activity_updated_at", _row_value(row, "updated_at", 0))
        return {
            "conversation_id": str(_row_value(row, "conversation_id", "") or ""),
            "team_id": str(_row_value(row, "team_id", "") or ""),
            "stable_session_id": str(_row_value(row, "stable_session_id", "") or ""),
            "title": str(_row_value(row, "title", "") or ""),
            "objective": str(_row_value(row, "objective", "") or ""),
            "workspace_id": str(_row_value(row, "workspace_id", "") or ""),
            "workspace_path": str(_row_value(row, "workspace_path", "") or ""),
            "status": str(_row_value(row, "status", "") or ""),
            "active_mission_id": str(_row_value(row, "active_mission_id", "") or ""),
            "created_by_user_id": str(_row_value(row, "created_by_user_id", "") or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": float(_row_value(row, "created_at", 0) or 0),
            "updated_at": float(updated_at or 0),
        }

    def _team_mission_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
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

    def _team_mission_node_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        metadata = _json_loads(row["metadata_json"], {})
        raw_kind = str(row["kind"] or "")
        kind = _normalize_node_kind(raw_kind)
        metadata = _metadata_with_normalized_node_kind(metadata, raw_kind=raw_kind, canonical_kind=kind)
        return {
            "node_id": str(row["node_id"] or ""),
            "mission_id": str(row["mission_id"] or ""),
            "kind": kind,
            "title": str(row["title"] or ""),
            "objective": str(row["objective"] or ""),
            "status": str(row["status"] or ""),
            "assignee_profile_id": str(row["assignee_profile_id"] or ""),
            "assignee_profile_version_id": str(row["assignee_profile_version_id"] or ""),
            "runtime_scope_key": str(row["runtime_scope_key"] or ""),
            "output_contract": _json_loads(row["output_contract_json"], {}),
            "metadata": metadata,
            **_assignee_public_fields(metadata),
            "position_x": float(row["position_x"] or 0),
            "position_y": float(row["position_y"] or 0),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def _team_mission_node_with_resolved_assignee(
        self,
        node: Dict[str, Any] | None,
        *,
        mission_metadata: Dict[str, Any] | None = None,
        leader_node: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if not isinstance(node, dict) or not node:
            return {}
        resolved_profile_id, resolved_profile_version_id, resolved_runtime_scope_key, resolved_metadata = _resolve_node_assignee(
            mission_id=str(node.get("mission_id") or ""),
            node_id=str(node.get("node_id") or ""),
            kind=_normalize_node_kind(node.get("kind")),
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
        resolved_node.update(_assignee_public_fields(resolved_metadata))
        return resolved_node

    def _team_mission_nodes_with_resolved_assignees(
        self,
        nodes: List[Dict[str, Any]],
        *,
        mission_metadata: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        leader_node = next((_node for _node in nodes if _normalize_node_kind(_node.get("kind")) == "root"), {})
        return [
            self._team_mission_node_with_resolved_assignee(
                node,
                mission_metadata=mission_metadata,
                leader_node=leader_node,
            )
            for node in nodes
        ]

    def _team_mission_edge_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
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

    def _team_mission_run_binding_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "mission_id": str(row["mission_id"] or ""),
            "node_id": str(row["node_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "session_id": str(row["session_id"] or ""),
            "runtime_session_id": str(row["runtime_session_id"] or ""),
            "runtime_scope_key": str(row["runtime_scope_key"] or ""),
            "role": str(row["role"] or ""),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def _team_mission_memory_item_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
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

    def _team_mission_memory_edge_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
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

    def upsert_team_mission_conversation(
        self,
        *,
        conversation_id: str,
        team_id: str = "",
        stable_session_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        status: str = "active",
        active_mission_id: str = "",
        created_by_user_id: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
        metadata: Dict[str, Any] | None = None,
        replace_title: bool = False,
        touch: bool = False,
    ) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        stable_session_id = _text(stable_session_id) or conversation_id
        now = time.time()
        created = float(created_at or now)
        requested_updated = float(updated_at if updated_at is not None else now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, title, created_at, updated_at FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata["conversation_id"] = conversation_id
            merged_metadata["stable_session_id"] = stable_session_id
            existing_title = _text(_row_value(existing, "title", ""))
            requested_title = _text(title)
            insert_title = requested_title or existing_title or "Team Mission"
            update_title = requested_title if (replace_title or not existing_title) else ""
            existing_updated = float(_row_value(existing, "updated_at", requested_updated) or requested_updated)
            update_updated = requested_updated if (updated_at is not None or touch or existing is None) else existing_updated
            conn.execute(
                """
                INSERT INTO team_mission_conversations (
                    conversation_id, team_id, stable_session_id, title, objective,
                    workspace_id, workspace_path, status, active_mission_id,
                    created_by_user_id, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    team_id = COALESCE(NULLIF(excluded.team_id, ''), team_id),
                    stable_session_id = excluded.stable_session_id,
                    title = COALESCE(NULLIF(?, ''), title),
                    objective = COALESCE(NULLIF(excluded.objective, ''), objective),
                    workspace_id = COALESCE(NULLIF(excluded.workspace_id, ''), workspace_id),
                    workspace_path = COALESCE(NULLIF(excluded.workspace_path, ''), workspace_path),
                    status = excluded.status,
                    active_mission_id = COALESCE(NULLIF(excluded.active_mission_id, ''), active_mission_id),
                    created_by_user_id = COALESCE(NULLIF(excluded.created_by_user_id, ''), created_by_user_id),
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    _text(team_id),
                    stable_session_id,
                    insert_title,
                    _text(objective),
                    _text(workspace_id),
                    _text(workspace_path),
                    _conversation_status(status),
                    _text(active_mission_id),
                    _text(created_by_user_id),
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                    float(_row_value(existing, "created_at", created) or created),
                    update_updated,
                    update_title,
                ),
            )
            return self._team_mission_conversation_from_row(conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def ensure_team_mission_conversation(
        self,
        *,
        conversation_id: str = "",
        stable_session_id: str = "",
        mission: Dict[str, Any] | None = None,
        mission_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        created_by_user_id: str = "",
        metadata: Dict[str, Any] | None = None,
        updated_at: float | None = None,
        touch: bool = False,
    ) -> Dict[str, Any]:
        mission = mission if isinstance(mission, dict) else {}
        mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
        merged_metadata = dict(mission_metadata)
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        resolved_mission_id = _text(mission_id or mission.get("mission_id"))
        explicit_conversation_id = (
            _text(conversation_id)
            or _text(mission.get("conversation_id"))
            or _conversation_id_from_metadata(merged_metadata)
        )
        resolved_conversation_id = (
            explicit_conversation_id
            or _text((self.get_team_mission_conversation_by_session(
                _stable_session_id_from_metadata(
                    merged_metadata,
                    _text(mission.get("leader_session_id") or mission.get("team_id") or resolved_mission_id),
                )
            ) or {}).get("conversation_id"))
            or resolved_mission_id
        )
        if not resolved_conversation_id:
            return {}
        resolved_stable_session_id = (
            _text(stable_session_id)
            or _stable_session_id_from_metadata(
                merged_metadata,
                _text(mission.get("leader_session_id") or mission.get("team_id") or resolved_conversation_id),
            )
        )
        merged_metadata["conversation_id"] = resolved_conversation_id
        merged_metadata["conversation_session_id"] = resolved_stable_session_id
        merged_metadata["stableTeamSessionId"] = resolved_stable_session_id
        conversation = self.upsert_team_mission_conversation(
            conversation_id=resolved_conversation_id,
            team_id=_text(team_id or mission.get("team_id")),
            stable_session_id=resolved_stable_session_id,
            title=_text(title),
            objective=_text(objective),
            workspace_id=_text(workspace_id or mission.get("workspace_id")),
            workspace_path=_text(workspace_path or mission.get("workspace_path")),
            status="active",
            active_mission_id=resolved_mission_id,
            created_by_user_id=_text(created_by_user_id),
            updated_at=updated_at,
            metadata=merged_metadata,
            touch=touch,
        )
        if resolved_mission_id:
            def _bind(conn: sqlite3.Connection) -> None:
                conn.execute(
                    """
                    UPDATE team_missions
                    SET conversation_id = ?,
                        leader_session_id = COALESCE(NULLIF(leader_session_id, ''), ?)
                    WHERE mission_id = ?
                    """,
                    (resolved_conversation_id, resolved_stable_session_id, resolved_mission_id),
                )

            self._execute_write(_bind)
        try:
            if resolved_stable_session_id and not self.get_session(resolved_stable_session_id):
                self.create_session(resolved_stable_session_id, source="team_mission", transient=False)
        except Exception:
            pass
        return conversation

    def get_team_mission_conversation(self, conversation_id: str) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        with self._lock:
            return self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()) or {}

    def get_team_mission_conversation_by_session(self, stable_session_id: str) -> Dict[str, Any]:
        stable_session_id = _text(stable_session_id)
        if not stable_session_id:
            return {}
        with self._lock:
            return self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE stable_session_id = ?",
                (stable_session_id,),
            ).fetchone()) or {}

    def resolve_team_mission_conversation(self, identifier: str) -> Dict[str, Any]:
        identifier = _text(identifier)
        if not identifier:
            return {}
        conversation = self.get_team_mission_conversation(identifier) or self.get_team_mission_conversation_by_session(identifier)
        if not conversation:
            with self._lock:
                mission = self._team_mission_from_row(self._conn.execute(
                    "SELECT * FROM team_missions WHERE mission_id = ?",
                    (identifier,),
                ).fetchone())
            if mission:
                conversation = self.ensure_team_mission_conversation(mission=mission)
        if not conversation:
            with self._lock:
                mission = self._team_mission_from_row(self._conn.execute(
                    "SELECT * FROM team_missions WHERE conversation_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (identifier,),
                ).fetchone())
            if mission:
                conversation = self.ensure_team_mission_conversation(
                    conversation_id=identifier,
                    mission=mission,
                )
        if not conversation:
            return {}
        mission_id = _text(conversation.get("active_mission_id"))
        if not mission_id:
            with self._lock:
                row = self._conn.execute(
                    "SELECT mission_id FROM team_missions WHERE conversation_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (_text(conversation.get("conversation_id")),),
                ).fetchone()
                mission_id = _text(_row_value(row, "mission_id", ""))
        graph = self.get_team_mission_graph(mission_id) if mission_id else {}
        return {
            "conversation": conversation,
            "mission": (graph.get("mission") if isinstance(graph, dict) else {}) or {},
            "graph": graph if isinstance(graph, dict) else {},
        }

    def list_team_mission_conversations(
        self,
        *,
        team_id: str = "",
        workspace_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if _text(team_id):
            clauses.append("team_id = ?")
            params.append(_text(team_id))
        if _text(workspace_id):
            clauses.append("workspace_id = ?")
            params.append(_text(workspace_id))
        if _text(status):
            clauses.append("status = ?")
            params.append(_conversation_status(status))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit or 100), 500))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT *,
                    MAX(
                        COALESCE(
                            (SELECT MAX(m.timestamp)
                             FROM messages m
                             WHERE m.session_id = team_mission_conversations.stable_session_id
                               AND m.active = 1),
                            0
                        ),
                        COALESCE(created_at, 0)
                    ) AS activity_updated_at
                FROM team_mission_conversations
                {where_sql}
                ORDER BY activity_updated_at DESC, created_at DESC, conversation_id ASC
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()
        return [
            conversation for conversation in (
                self._team_mission_conversation_from_row(row)
                for row in rows
            ) if conversation is not None
        ]

    def rename_team_mission_conversation(self, identifier: str, title: str) -> Dict[str, Any]:
        return _rename_team_mission_conversation(self, identifier, title)

    def delete_team_mission_conversation(self, identifier: str) -> Dict[str, Any]:
        return _delete_team_mission_conversation(self, identifier)

    def upsert_team_mission(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        mode: str = "supervised_mission",
        status: str = "planning",
        leader_session_id: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
        completed_at: float | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        now = time.time()
        created = float(created_at or now)
        updated = float(updated_at or now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, created_at, conversation_id FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            candidate_stable_session_id = _stable_session_id_from_metadata(
                merged_metadata,
                _text(leader_session_id or team_id or mission_id),
            )
            conversation_by_session = conn.execute(
                "SELECT conversation_id FROM team_mission_conversations WHERE stable_session_id = ?",
                (candidate_stable_session_id,),
            ).fetchone() if candidate_stable_session_id else None
            resolved_conversation_id = (
                _text(conversation_id)
                or _text(_row_value(existing, "conversation_id", ""))
                or _conversation_id_from_metadata(merged_metadata)
                or _text(_row_value(conversation_by_session, "conversation_id", ""))
                or mission_id
            )
            if resolved_conversation_id:
                merged_metadata["conversation_id"] = resolved_conversation_id
            conn.execute(
                """
                INSERT INTO team_missions (
                    mission_id, conversation_id, team_id, title, objective, workspace_id, workspace_path,
                    mode, status, leader_session_id, created_at, updated_at, completed_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    conversation_id = COALESCE(NULLIF(excluded.conversation_id, ''), conversation_id),
                    team_id = excluded.team_id,
                    title = excluded.title,
                    objective = excluded.objective,
                    workspace_id = excluded.workspace_id,
                    workspace_path = excluded.workspace_path,
                    mode = excluded.mode,
                    status = excluded.status,
                    leader_session_id = COALESCE(NULLIF(excluded.leader_session_id, ''), leader_session_id),
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    mission_id,
                    resolved_conversation_id,
                    str(team_id or ""),
                    str(title or ""),
                    str(objective or ""),
                    str(workspace_id or ""),
                    str(workspace_path or ""),
                    str(mode or "supervised_mission"),
                    str(status or "planning"),
                    str(leader_session_id or ""),
                    float(_row_value(existing, "created_at", created) or created),
                    updated,
                    completed_at,
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                ),
            )
            return self._team_mission_from_row(conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()) or {}

        mission = self._execute_write(_do)
        if mission:
            self.ensure_team_mission_conversation(mission=mission)
            refreshed = self.get_team_mission_graph(mission_id).get("mission")
            return refreshed or mission
        return mission

    def initialize_team_mission_from_strategy(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        mode: str = "supervised_mission",
        members: List[Dict[str, Any]] | None = None,
        graph_payload: Dict[str, Any] | None = None,
        leader_session_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create a Team Mission through the Hermes mode strategy contract.

        This is the native entry point for product modes. Callers provide the
        requested mode and optional manual graph payload; Hermes chooses the
        initial graph/status through ``TeamMissionModeStrategy`` and persists
        it in the Team Mission tables.
        """
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        strategy = strategy_for_mode(mode)
        patch = strategy.initialize_graph(
            mission_id=mission_id,
            title=str(title or ""),
            objective=str(objective or ""),
            members=members or [],
            graph_payload=graph_payload or {},
        )
        mission_metadata = _mission_metadata_with_members({
            "mode_strategy": strategy.mode,
            "start_leader": patch.start_leader,
            "auto_start_ready_nodes": patch.auto_start_ready_nodes,
            "requires_whole_graph_approval": patch.requires_whole_graph_approval,
            **(patch.metadata or {}),
        }, members)
        if isinstance(metadata, dict):
            mission_metadata.update(metadata)
            mission_metadata = _mission_metadata_with_members(mission_metadata, members)
        self.upsert_team_mission(
            mission_id=mission_id,
            conversation_id=conversation_id,
            team_id=team_id,
            title=title,
            objective=objective,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            mode=strategy.mode,
            status=patch.mission_status,
            leader_session_id=leader_session_id,
            metadata=mission_metadata,
        )
        for node in patch.nodes:
            self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=node.node_id,
                kind=node.kind,
                title=node.title,
                objective=node.objective,
                status=node.status,
                assignee_profile_id=node.assignee_profile_id,
                assignee_profile_version_id=node.assignee_profile_version_id,
                runtime_scope_key=node.runtime_scope_key,
                output_contract=node.output_contract,
                metadata=node.metadata,
                position_x=node.position_x,
                position_y=node.position_y,
            )
        for edge in patch.edges:
            self.upsert_team_mission_edge(
                mission_id=mission_id,
                edge_id=edge.edge_id,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                kind=edge.kind,
                metadata=edge.metadata,
            )
        return self.get_team_mission_graph(mission_id)

    def upsert_team_mission_node(
        self,
        *,
        mission_id: str,
        node_id: str,
        kind: str = "worker",
        title: str = "",
        objective: str = "",
        status: str = "todo",
        assignee_profile_id: str = "",
        assignee_profile_version_id: str = "",
        runtime_scope_key: str = "",
        output_contract: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
        position_x: float = 0,
        position_y: float = 0,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        now = time.time()
        created = float(created_at or now)
        updated = float(updated_at or now)
        raw_kind = str(kind or "worker")
        canonical_kind = _normalize_node_kind(raw_kind)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM team_mission_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata = _metadata_with_normalized_node_kind(
                merged_metadata if isinstance(merged_metadata, dict) else {},
                raw_kind=raw_kind,
                canonical_kind=canonical_kind,
            )
            mission_row = conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            mission_metadata = _json_loads(_row_value(mission_row, "metadata_json", ""), {})
            leader_row = conn.execute(
                """
                SELECT * FROM team_mission_nodes
                 WHERE mission_id = ? AND kind = ?
                 ORDER BY created_at ASC
                 LIMIT 1
                """,
                (mission_id, "root"),
            ).fetchone()
            existing_node = self._team_mission_node_from_row(existing) or {}
            leader_node = self._team_mission_node_from_row(leader_row) or {}
            resolved_profile_id, resolved_profile_version_id, resolved_runtime_scope_key, resolved_metadata = _resolve_node_assignee(
                mission_id=mission_id,
                node_id=node_id,
                kind=canonical_kind,
                incoming_profile_id=str(assignee_profile_id or ""),
                incoming_profile_version_id=str(assignee_profile_version_id or ""),
                incoming_runtime_scope_key=str(runtime_scope_key or ""),
                metadata=merged_metadata if isinstance(merged_metadata, dict) else {},
                mission_metadata=mission_metadata if isinstance(mission_metadata, dict) else {},
                existing_node=existing_node,
                leader_node=leader_node,
            )
            conn.execute(
                """
                INSERT INTO team_mission_nodes (
                    node_id, mission_id, kind, title, objective, status,
                    assignee_profile_id, assignee_profile_version_id, runtime_scope_key,
                    output_contract_json, metadata_json, position_x, position_y,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    kind = excluded.kind,
                    title = excluded.title,
                    objective = excluded.objective,
                    status = excluded.status,
                    assignee_profile_id = excluded.assignee_profile_id,
                    assignee_profile_version_id = excluded.assignee_profile_version_id,
                    runtime_scope_key = excluded.runtime_scope_key,
                    output_contract_json = excluded.output_contract_json,
                    metadata_json = excluded.metadata_json,
                    position_x = excluded.position_x,
                    position_y = excluded.position_y,
                    updated_at = excluded.updated_at
                """,
                (
                    node_id,
                    mission_id,
                    canonical_kind,
                    str(title or ""),
                    str(objective or ""),
                    str(status or "todo"),
                    resolved_profile_id,
                    resolved_profile_version_id,
                    resolved_runtime_scope_key,
                    _json_dumps(output_contract or {}),
                    _json_dumps(resolved_metadata),
                    float(position_x or 0),
                    float(position_y or 0),
                    float(_row_value(existing, "created_at", created) or created),
                    updated,
                ),
            )
            return self._team_mission_node_from_row(conn.execute(
                "SELECT * FROM team_mission_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def get_team_mission_node(self, mission_id: str, node_id: str) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            mission_row = self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            leader_row = self._conn.execute(
                """
                SELECT * FROM team_mission_nodes
                 WHERE mission_id = ? AND kind = ?
                 ORDER BY created_at ASC
                 LIMIT 1
                """,
                (mission_id, "root"),
            ).fetchone()
        mission = self._team_mission_from_row(mission_row) or {}
        return self._team_mission_node_with_resolved_assignee(
            self._team_mission_node_from_row(row) or {},
            mission_metadata=dict(mission.get("metadata") or {}),
            leader_node=self._team_mission_node_from_row(leader_row) or {},
        )

    def claim_team_mission_node_start(
        self,
        *,
        mission_id: str,
        node_id: str,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Atomically claim a ready node before submitting its runtime run."""
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            node = self._team_mission_node_from_row(row)
            if not node:
                return {}
            if str(node.get("status") or "") != "ready":
                return node
            merged_metadata = dict(node.get("metadata") or {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata["start_claimed_at"] = now
            cursor = conn.execute(
                """
                UPDATE team_mission_nodes
                   SET status = ?,
                       metadata_json = ?,
                       updated_at = ?
                 WHERE mission_id = ?
                   AND node_id = ?
                   AND status = ?
                """,
                (
                    "starting",
                    _json_dumps(merged_metadata),
                    now,
                    mission_id,
                    node_id,
                    "ready",
                ),
            )
            if cursor.rowcount <= 0:
                return self._team_mission_node_from_row(conn.execute(
                    "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                    (mission_id, node_id),
                ).fetchone()) or {}
            return self._team_mission_node_from_row(conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def upsert_team_mission_edge(
        self,
        *,
        mission_id: str,
        from_node_id: str,
        to_node_id: str,
        edge_id: str = "",
        kind: str = "depends_on",
        metadata: Dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        from_node_id = str(from_node_id or "").strip()
        to_node_id = str(to_node_id or "").strip()
        normalized_edge_id = str(edge_id or f"{mission_id}:{from_node_id}:{to_node_id}:{kind}").strip()
        if not mission_id or not from_node_id or not to_node_id:
            return {}
        created = float(created_at or time.time())

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT INTO team_mission_edges (
                    edge_id, mission_id, from_node_id, to_node_id, kind, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    from_node_id = excluded.from_node_id,
                    to_node_id = excluded.to_node_id,
                    kind = excluded.kind,
                    metadata_json = excluded.metadata_json
                """,
                (
                    normalized_edge_id,
                    mission_id,
                    from_node_id,
                    to_node_id,
                    str(kind or "depends_on"),
                    _json_dumps(metadata or {}),
                    created,
                ),
            )
            return self._team_mission_edge_from_row(conn.execute(
                "SELECT * FROM team_mission_edges WHERE edge_id = ?",
                (normalized_edge_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def apply_team_mission_strategy_actions(
        self,
        *,
        mission_id: str,
        actions: TeamMissionStrategyActions,
        run_id: str = "",
        event_source: str = "strategy",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        if actions.mission_status:
            self.upsert_team_mission(
                mission_id=mission_id,
                team_id=str(mission.get("team_id") or ""),
                title=str(mission.get("title") or ""),
                objective=str(mission.get("objective") or ""),
                workspace_id=str(mission.get("workspace_id") or ""),
                workspace_path=str(mission.get("workspace_path") or ""),
                mode=str(mission.get("mode") or ""),
                status=actions.mission_status,
                leader_session_id=str(mission.get("leader_session_id") or ""),
                metadata=dict(mission.get("metadata") or {}),
            )
        created_nodes = []
        for node in actions.nodes:
            created_nodes.append(
                self.upsert_team_mission_node(
                    mission_id=mission_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    title=node.title,
                    objective=node.objective,
                    status=node.status,
                    assignee_profile_id=node.assignee_profile_id,
                    assignee_profile_version_id=node.assignee_profile_version_id,
                    runtime_scope_key=node.runtime_scope_key,
                    output_contract=node.output_contract,
                    metadata=node.metadata,
                    position_x=node.position_x,
                    position_y=node.position_y,
                )
            )
        created_edges = []
        for edge in actions.edges:
            created_edges.append(
                self.upsert_team_mission_edge(
                    mission_id=mission_id,
                    edge_id=edge.edge_id,
                    from_node_id=edge.from_node_id,
                    to_node_id=edge.to_node_id,
                    kind=edge.kind,
                    metadata=edge.metadata,
                )
            )
        if run_id:
            for event in actions.events:
                if isinstance(event, dict):
                    self.append_team_mission_run_event(
                        mission_id=mission_id,
                        run_id=run_id,
                        event=event,
                    )
            self.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=run_id,
                event={
                    "type": "mission.strategy.actions",
                    "payload": {
                        "source": event_source,
                        "mission_status": actions.mission_status,
                        "nodes": created_nodes,
                        "edges": created_edges,
                        "start_node_ids": list(actions.start_node_ids),
                        "approval_requests": list(actions.approval_requests),
                        "auto_start_ready_nodes": actions.auto_start_ready_nodes,
                    },
                },
            )
        return self.get_team_mission_graph(mission_id)

    def complete_team_mission_plan(
        self,
        *,
        mission_id: str,
        run_id: str = "",
        task_id: str = "",
        event_source: str = "plan.complete",
    ) -> Dict[str, Any]:
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        strategy = strategy_for_mode(str(mission.get("mode") or "supervised_mission"))
        normalized_task_id = _text(task_id)
        if not normalized_task_id and run_id:
            binding = self.get_team_mission_run_binding(run_id)
            bound_node = self.get_team_mission_node(
                str(binding.get("mission_id") or mission_id),
                str(binding.get("node_id") or ""),
            )
            normalized_task_id = _task_id_from_node_and_binding(bound_node, binding)
        planned_nodes = tuple(
            _node_spec_from_graph_node(node)
            for node in graph.get("nodes", [])
            if isinstance(node, dict)
            and str(node.get("kind") or "") not in {"root", "approval_gate"}
            and _node_matches_task(node, normalized_task_id)
        )
        planned_node_ids = {
            node.node_id
            for node in planned_nodes
            if node.node_id
        }
        planned_edges = tuple(
            _edge_spec_from_graph_edge(edge)
            for edge in graph.get("edges", [])
            if isinstance(edge, dict)
            and _edge_matches_task(edge, planned_node_ids, normalized_task_id)
        )
        actions = strategy.on_plan_completed(
            mission_id=mission_id,
            planned_nodes=planned_nodes,
            planned_edges=planned_edges,
        )
        actions = _strategy_actions_with_task_id(actions, normalized_task_id)
        updated_graph = self.apply_team_mission_strategy_actions(
            mission_id=mission_id,
            actions=actions,
            run_id=run_id,
            event_source=event_source,
        )
        reduced = self.reduce_team_mission_graph(mission_id)
        return {
            "mission_id": mission_id,
            "mission_status": actions.mission_status,
            "approval_requests": list(actions.approval_requests),
            "auto_start_ready_nodes": actions.auto_start_ready_nodes,
            "actions": actions,
            "graph": reduced.get("graph") if isinstance(reduced, dict) and reduced.get("graph") else updated_graph,
        }

    def reject_team_mission_plan(
        self,
        *,
        mission_id: str,
        task_id: str = "",
        rejected_by: str = "",
        reason: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        normalized_task_id = _text(task_id)
        if not normalized_task_id:
            root_nodes = [
                node for node in nodes
                if str(node.get("kind") or "") == "root"
                and _task_id_from_node_and_binding(node)
            ]
            root_nodes.sort(key=lambda node: float(node.get("created_at") or node.get("updated_at") or 0))
            normalized_task_id = _task_id_from_node_and_binding(root_nodes[-1]) if root_nodes else ""
        selected_nodes = [
            node for node in nodes
            if (
                normalized_task_id
                and _task_id_from_node_and_binding(node) == normalized_task_id
            )
        ]
        if not selected_nodes:
            selected_nodes = [
                node for node in nodes
                if str(node.get("kind") or "") in {"root", "approval_gate"}
                or str(node.get("status") or "") in {"ready", "todo", "waiting_dependency", "blocked_waiting_dependency", "waiting_approval"}
            ]
        canceled_nodes = []
        for node in selected_nodes:
            canceled_nodes.append(self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=str(node.get("node_id") or ""),
                kind=str(node.get("kind") or "worker"),
                title=str(node.get("title") or ""),
                objective=str(node.get("objective") or ""),
                status="cancelled",
                assignee_profile_id=str(node.get("assignee_profile_id") or ""),
                assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
                runtime_scope_key=str(node.get("runtime_scope_key") or ""),
                output_contract=dict(node.get("output_contract") or {}),
                metadata={
                    **dict(node.get("metadata") or {}),
                    "rejected_by": _text(rejected_by),
                    "rejected_reason": _text(reason),
                    **({"task_id": normalized_task_id} if normalized_task_id else {}),
                },
                position_x=float(node.get("position_x") or 0),
                position_y=float(node.get("position_y") or 0),
            ))
        self.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="draft",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata=dict(mission.get("metadata") or {}),
        )
        event = {
            "type": "mission.plan.rejected",
            "payload": {
                "task_id": normalized_task_id,
                "node_ids": [str(node.get("node_id") or "") for node in canceled_nodes],
                "rejected_by": _text(rejected_by),
                "reason": _text(reason),
            },
        }
        if run_id:
            self.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=run_id,
                event=event,
            )
        return {
            "mission_id": mission_id,
            "task_id": normalized_task_id,
            "canceled_nodes": canceled_nodes,
            "graph": self.get_team_mission_graph(mission_id),
        }

    def cancel_team_mission(
        self,
        *,
        mission_id: str,
        canceled_by: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        mission_status = _text(mission.get("status")).lower()
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        bindings = [binding for binding in graph.get("run_bindings", []) if isinstance(binding, dict)]
        cancel_run_bindings: list[Dict[str, Any]] = []
        active_run_ids: set[str] = set()
        for binding in bindings:
            run_id = _text(binding.get("run_id"))
            if not run_id:
                continue
            run = self.get_run(run_id) if hasattr(self, "get_run") else None
            run_status = _text((run or {}).get("status")).lower()
            if run_status in _ACTIVE_RUN_STATUSES:
                active_run_ids.add(run_id)
                cancel_run_bindings.append(binding)
        if mission_status in _TERMINAL_MISSION_STATUSES:
            return {
                "mission_id": mission_id,
                "mission_status": mission_status or "cancelled",
                "canceled_nodes": [],
                "cancel_run_bindings": cancel_run_bindings,
                "graph": graph,
            }
        canceled_at = time.time()
        cancellation_metadata = {
            "canceled_by": _text(canceled_by),
            "cancel_reason": _text(reason),
            "canceled_at": canceled_at,
        }
        canceled_nodes = []
        canceled_node_ids: set[str] = set()
        for node in nodes:
            node_id = _text(node.get("node_id"))
            node_status = _text(node.get("status")).lower()
            if not node_id or node_status in _TERMINAL_NODE_STATUSES:
                continue
            if node_status not in _CANCELLABLE_NODE_STATUSES and node_status:
                continue
            canceled_node_ids.add(node_id)
            canceled_nodes.append(self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=node_id,
                kind=str(node.get("kind") or "worker"),
                title=str(node.get("title") or ""),
                objective=str(node.get("objective") or ""),
                status="cancelled",
                assignee_profile_id=str(node.get("assignee_profile_id") or ""),
                assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
                runtime_scope_key=str(node.get("runtime_scope_key") or ""),
                output_contract=dict(node.get("output_contract") or {}),
                metadata={
                    **dict(node.get("metadata") or {}),
                    **cancellation_metadata,
                },
                position_x=float(node.get("position_x") or 0),
                position_y=float(node.get("position_y") or 0),
            ))
        for binding in bindings:
            run_id = _text(binding.get("run_id"))
            node_id = _text(binding.get("node_id"))
            if not run_id:
                continue
            if run_id in active_run_ids or node_id in canceled_node_ids:
                if not any(_text(item.get("run_id")) == run_id for item in cancel_run_bindings):
                    cancel_run_bindings.append(binding)
        self.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="cancelled",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata={
                **dict(mission.get("metadata") or {}),
                **cancellation_metadata,
            },
        )
        return {
            "mission_id": mission_id,
            "mission_status": "cancelled",
            "canceled_nodes": canceled_nodes,
            "cancel_run_bindings": cancel_run_bindings,
            "graph": self.get_team_mission_graph(mission_id),
        }

    def bind_team_mission_run(
        self,
        *,
        mission_id: str,
        node_id: str,
        run_id: str,
        session_id: str,
        role: str = "worker",
        runtime_session_id: str = "",
        runtime_scope_key: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        run_id = str(run_id or "").strip()
        session_id = str(session_id or "").strip()
        if not mission_id or not run_id or not session_id:
            return {}
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, created_at FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            conn.execute(
                """
                INSERT INTO team_mission_run_bindings (
                    mission_id, node_id, run_id, session_id, runtime_session_id,
                    runtime_scope_key, role, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    node_id = excluded.node_id,
                    session_id = excluded.session_id,
                    runtime_session_id = excluded.runtime_session_id,
                    runtime_scope_key = excluded.runtime_scope_key,
                    role = excluded.role,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    mission_id,
                    str(node_id or ""),
                    run_id,
                    session_id,
                    str(runtime_session_id or ""),
                    str(runtime_scope_key or ""),
                    str(role or "worker"),
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                    float(_row_value(existing, "created_at", now) or now),
                    now,
                ),
            )
            return self._team_mission_run_binding_from_row(conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def get_team_mission_run_binding(self, run_id: str) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._team_mission_run_binding_from_row(row) or {}

    def reduce_team_mission_run_event(self, *, run_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            return {}
        binding = self.get_team_mission_run_binding(run_id)
        if not binding:
            return {}
        event_type = str((event or {}).get("type") or "").strip()
        payload = event.get("payload") if isinstance((event or {}).get("payload"), dict) else {}
        next_status = ""
        if event_type == "error":
            next_status = "failed"
        elif event_type == "message.complete":
            status = str(payload.get("status") or "").strip().lower()
            if status in {"cancelled", "canceled"}:
                next_status = "cancelled"
            elif status == "interrupted":
                next_status = "interrupted"
            elif status in {"failed", "error"}:
                next_status = "failed"
            else:
                next_status = "completed"
        if not next_status:
            return {}
        node = self.get_team_mission_node(str(binding.get("mission_id") or ""), str(binding.get("node_id") or ""))
        if not node:
            return {}
        updated = self.upsert_team_mission_node(
            mission_id=str(binding.get("mission_id") or ""),
            node_id=str(binding.get("node_id") or ""),
            kind=str(node.get("kind") or "worker"),
            title=str(node.get("title") or ""),
            objective=str(node.get("objective") or ""),
            status=next_status,
            assignee_profile_id=str(node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
            runtime_scope_key=str(node.get("runtime_scope_key") or binding.get("runtime_scope_key") or ""),
            output_contract=dict(node.get("output_contract") or {}),
            metadata={
                **dict(node.get("metadata") or {}),
                "last_run_id": run_id,
                "last_run_terminal_event": event_type,
            },
            position_x=float(node.get("position_x") or 0),
            position_y=float(node.get("position_y") or 0),
        )
        self.reduce_team_mission_graph(str(binding.get("mission_id") or ""))
        compile_mode = "final"
        if next_status in {"cancelled", "interrupted"}:
            compile_mode = "canceled"
        elif next_status in {"failed"}:
            compile_mode = "blocked"
        try:
            self.compile_team_mission_memory(
                mission_id=str(binding.get("mission_id") or ""),
                mode=compile_mode,
                source_run_ids=[run_id],
                emit_event=True,
            )
        except Exception:
            pass
        return updated

    def append_team_mission_run_event(
        self,
        *,
        mission_id: str,
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        run_id = str(run_id or "").strip()
        if not mission_id or not run_id:
            return {}
        with self._lock:
            binding = self._conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE mission_id = ? AND run_id = ?",
                (mission_id, run_id),
            ).fetchone()
        if binding is None:
            return {}
        frame = dict(event or {})
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        mission_payload = {
            **payload,
            "mission_id": mission_id,
            "node_id": str(binding["node_id"] or ""),
        }
        frame.update({
            "run_id": run_id,
            "session_id": str(frame.get("session_id") or binding["runtime_session_id"] or ""),
            "stored_session_id": str(binding["session_id"] or ""),
            "runtime_scope_key": str(frame.get("runtime_scope_key") or binding["runtime_scope_key"] or binding["session_id"] or ""),
            "payload": mission_payload,
        })
        saved = self.append_run_event(str(binding["session_id"] or ""), frame)
        self.reduce_team_mission_run_event(run_id=run_id, event=saved or frame)
        try:
            _mirror_team_mission_event(
                self,
                mission_id=mission_id,
                binding=self._team_mission_run_binding_from_row(binding) or {},
                event=saved or frame,
                source="team_mission_run_event",
            )
        except Exception:
            pass
        return saved

    def get_team_mission_graph(self, mission_id: str) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        with self._lock:
            mission = self._team_mission_from_row(self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())
            if mission is None:
                return {}
            conversation = self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (_text(mission.get("conversation_id")),),
            ).fetchone()) if _text(mission.get("conversation_id")) else None
            nodes = [
                node for node in (
                    self._team_mission_node_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_nodes WHERE mission_id = ? ORDER BY created_at ASC, node_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if node is not None
            ]
            nodes = self._team_mission_nodes_with_resolved_assignees(
                nodes,
                mission_metadata=dict(mission.get("metadata") or {}),
            )
            edges = [
                edge for edge in (
                    self._team_mission_edge_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_edges WHERE mission_id = ? ORDER BY created_at ASC, edge_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if edge is not None
            ]
            run_bindings = [
                binding for binding in (
                    self._team_mission_run_binding_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_run_bindings WHERE mission_id = ? ORDER BY created_at ASC, run_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if binding is not None
            ]
        return {
            "mission": mission,
            "conversation": conversation or {},
            "nodes": nodes,
            "edges": edges,
            "run_bindings": run_bindings,
        }

    def _team_mission_memory_context(self, mission: Dict[str, Any]) -> Dict[str, Any]:
        return _memory_state.team_mission_memory_context(self, mission)

    def upsert_team_mission_memory_item(
        self,
        *,
        memory_id: str = "",
        team_id: str,
        mission_id: str,
        conversation_session_id: str,
        task_id: str = "",
        scope: str = "mission_task",
        kind: str = "summary",
        content: str,
        structured_payload: Dict[str, Any] | None = None,
        source_node_ids: List[str] | None = None,
        source_run_ids: List[str] | None = None,
        artifact_refs: List[Dict[str, Any]] | None = None,
        workspace_refs: List[Dict[str, Any]] | None = None,
        confidence: float = 0.75,
        visibility: str = "team",
        status: str = _MEMORY_COMMITTED_STATUS,
        created_at: float | None = None,
        updated_at: float | None = None,
        invalidated_at: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.upsert_team_mission_memory_item(
            self,
            memory_id=memory_id,
            team_id=team_id,
            mission_id=mission_id,
            conversation_session_id=conversation_session_id,
            task_id=task_id,
            scope=scope,
            kind=kind,
            content=content,
            structured_payload=structured_payload,
            source_node_ids=source_node_ids,
            source_run_ids=source_run_ids,
            artifact_refs=artifact_refs,
            workspace_refs=workspace_refs,
            confidence=confidence,
            visibility=visibility,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            invalidated_at=invalidated_at,
        )

    def list_team_mission_memory_items(
        self,
        *,
        mission_id: str = "",
        conversation_session_id: str = "",
        team_id: str = "",
        task_id: str = "",
        kinds: List[str] | None = None,
        statuses: List[str] | None = None,
        visibility: List[str] | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return _memory_state.list_team_mission_memory_items(
            self,
            mission_id=mission_id,
            conversation_session_id=conversation_session_id,
            team_id=team_id,
            task_id=task_id,
            kinds=kinds,
            statuses=statuses,
            visibility=visibility,
            include_deleted=include_deleted,
            limit=limit,
        )

    def update_team_mission_memory_item(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        structured_payload: Dict[str, Any] | None = None,
        visibility: str | None = None,
        status: str | None = None,
        confidence: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.update_team_mission_memory_item(
            self,
            memory_id,
            content=content,
            structured_payload=structured_payload,
            visibility=visibility,
            status=status,
            confidence=confidence,
        )

    def delete_team_mission_memory_item(self, memory_id: str) -> Dict[str, Any]:
        return _memory_state.delete_team_mission_memory_item(self, memory_id)

    def upsert_team_mission_memory_edge(
        self,
        *,
        from_memory_id: str,
        to_memory_id: str = "",
        relation: str,
        metadata: Dict[str, Any] | None = None,
        edge_id: str = "",
        created_at: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.upsert_team_mission_memory_edge(
            self,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation=relation,
            metadata=metadata,
            edge_id=edge_id,
            created_at=created_at,
        )

    def list_team_mission_memory_edges(
        self,
        *,
        from_memory_id: str = "",
        to_memory_id: str = "",
        relation: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return _memory_state.list_team_mission_memory_edges(
            self,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation=relation,
            limit=limit,
        )

    def compile_team_mission_memory(
        self,
        *,
        mission_id: str,
        task_id: str = "",
        mode: str = "final",
        source_run_ids: List[str] | None = None,
        emit_event: bool = True,
    ) -> Dict[str, Any]:
        return _memory_state.compile_team_mission_memory(
            self,
            mission_id=mission_id,
            task_id=task_id,
            mode=mode,
            source_run_ids=source_run_ids,
            emit_event=emit_event,
        )

    def build_team_mission_memory_pack(
        self,
        *,
        mission_id: str,
        objective: str = "",
        workspace_id: str = "",
        limit: int = 8,
        include_team_scope: bool = False,
    ) -> Dict[str, Any]:
        return _memory_state.build_team_mission_memory_pack(
            self,
            mission_id=mission_id,
            objective=objective,
            workspace_id=workspace_id,
            limit=limit,
            include_team_scope=include_team_scope,
        )

    def build_team_mission_memory_slice(
        self,
        *,
        mission_id: str,
        node_id: str,
        objective: str = "",
        limit: int = 5,
        include_team_scope: bool = False,
    ) -> Dict[str, Any]:
        return _memory_state.build_team_mission_memory_slice(
            self,
            mission_id=mission_id,
            node_id=node_id,
            objective=objective,
            limit=limit,
            include_team_scope=include_team_scope,
        )

    def reduce_team_mission_graph(self, mission_id: str) -> Dict[str, Any]:
        return _graph_state.reduce_team_mission_graph(self, mission_id)

    def _ensure_team_mission_finalizers(
        self,
        *,
        mission: Dict[str, Any],
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return _graph_state.ensure_team_mission_finalizers(
            self,
            mission=mission,
            nodes=nodes,
            edges=edges,
        )

    def list_team_mission_run_events(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT (
                    e.id * {_TEAM_MISSION_EVENT_SEQ_FACTOR}
                    + COALESCE(e.seq, 0)
                ) AS mission_event_seq,
                e.event_json
                FROM run_events e
                INNER JOIN team_mission_run_bindings b
                    ON b.run_id = e.run_id
                   AND b.session_id = e.session_id
                WHERE b.mission_id = ?
                  AND (
                    e.id * {_TEAM_MISSION_EVENT_SEQ_FACTOR}
                    + COALESCE(e.seq, 0)
                  ) > ?
                ORDER BY mission_event_seq ASC
                LIMIT ?
                """,
                (mission_id, int(after_seq or 0), max(1, min(int(limit or 2000), 10000))),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            event = _json_loads(row["event_json"], {})
            if isinstance(event, dict):
                source_seq = int(event.get("seq") or 0)
                mission_seq = int(row["mission_event_seq"] or 0)
                event["source_seq"] = source_seq
                event["team_mission_event_seq"] = mission_seq
                event["seq"] = mission_seq
                events.append(event)
        return events
