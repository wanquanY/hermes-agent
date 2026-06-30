from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *


class SessionDBTeamMissionRowsMixin:
    def _team_mission_runtime_event_identity(
        self,
        *,
        mission: Dict[str, Any] | None,
        node: Dict[str, Any] | None,
        binding: Dict[str, Any] | None,
    ) -> Dict[str, str]:
        return _team_mission_runtime_event_identity(
            mission=mission,
            node=node,
            binding=binding,
        )

    def _team_mission_conversation_from_row(self, row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
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
            "stable_session_id": str(_row_value(row, "stable_session_id", "") or ""),
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
            "canonical_node_id": _text(_row_value(row, "canonical_node_id", "")) or _conversation_graph_node_id(
                _text(_row_value(row, "mission_id", "")),
                _text(_row_value(row, "node_id", "")),
            ),
            "canonicalNodeId": _text(_row_value(row, "canonical_node_id", "")) or _conversation_graph_node_id(
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
            "runtime_stable_session_id": _text(_row_value(row, "runtime_stable_session_id", "")),
            "runtimeStableSessionId": _text(_row_value(row, "runtime_stable_session_id", "")),
            "runtime_session_id": _text(_row_value(row, "runtime_session_id", "")),
            "runtimeSessionId": _text(_row_value(row, "runtime_session_id", "")),
            "runtime_scope_key": str(row["runtime_scope_key"] or ""),
            "runtimeScopeKey": str(row["runtime_scope_key"] or ""),
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

    def _team_mission_latest_run_bindings_by_node(
        self,
        bindings: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
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

    def _team_mission_node_with_runtime_binding(
        self,
        node: Dict[str, Any] | None,
        binding: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        if not isinstance(node, dict) or not node:
            return {}
        if not isinstance(binding, dict) or not binding:
            return node
        node_id = _text(node.get("node_id"))
        if node_id and _text(binding.get("node_id")) and node_id != _text(binding.get("node_id")):
            return node

        session_id = _text(node.get("runtime_stable_session_id")) or _text(binding.get("session_id"))
        runtime_session_id = _text(node.get("runtime_session_id")) or _text(binding.get("runtime_session_id"))
        runtime_scope_key = _text(node.get("runtime_scope_key")) or _text(binding.get("runtime_scope_key"))
        run_id = _text(binding.get("run_id"))
        # CR-P3.3: graph identity only; for speaker use participant_id.
        canonical_node_id = _text(node.get("canonical_node_id")) or _conversation_graph_node_id(
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
            "canonical_node_id": canonical_node_id,
            "canonicalNodeId": canonical_node_id,
            "task_frame_id": task_frame_id,
            "taskFrameId": task_frame_id,
            "run_id": run_id,
            "runId": run_id,
            "session_id": session_id,
            "sessionId": session_id,
            "stable_session_id": session_id,
            "stableSessionId": session_id,
            "stored_session_id": session_id,
            "storedSessionId": session_id,
            "actual_stable_session_id": session_id,
            "actualStableSessionId": session_id,
            "runtime_stable_session_id": session_id,
            "runtimeStableSessionId": session_id,
            "runtime_session_id": runtime_session_id,
            "runtimeSessionId": runtime_session_id,
            "runtime_scope_key": runtime_scope_key,
            "runtimeScopeKey": runtime_scope_key,
            "runtime_binding": binding,
            "runtimeBinding": binding,
        }

    def _team_mission_nodes_with_runtime_bindings(
        self,
        nodes: List[Dict[str, Any]],
        bindings: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        latest_by_node = self._team_mission_latest_run_bindings_by_node(bindings)
        return [
            self._team_mission_node_with_runtime_binding(
                node,
                latest_by_node.get(_text(node.get("node_id"))),
            )
            for node in nodes
        ]

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

    def _team_mission_message_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        if row is None:
            return {}
        message = dict(row)
        if "content" in message:
            message["content"] = self._decode_content(message["content"])
        if message.get("metadata_json"):
            metadata = _json_loads(message.get("metadata_json"), None)
            if isinstance(metadata, dict):
                message["metadata"] = metadata
        return message

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

    def _team_mission_deliverable_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        return _deliverable_state.row_to_deliverable(row)

    def _team_mission_result_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        return _result_state.row_to_mission_result(row)
