from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *


class TeamMissionViewMixin:
    def get_team_mission_graph(self, mission_id: str) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        with self._lock:
            mission = self.team_mission_rows.mission_from_row(self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())
            if mission is None:
                return {}
            conversation = self.team_mission_rows.conversation_from_row(self._conn.execute(
                f"""
                SELECT team_mission_conversations.*,
                       {self._PROJECTED_ACTIVE_MISSION_ID_SQL}
                FROM team_mission_conversations
                WHERE conversation_id = ?
                """,
                (_text(mission.get("conversation_id")),),
            ).fetchone()) if _text(mission.get("conversation_id")) else None
            nodes = [
                node for node in (
                    self.team_mission_rows.node_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_nodes WHERE mission_id = ? ORDER BY created_at ASC, node_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if node is not None
            ]
            nodes = self.team_mission_rows.nodes_with_resolved_assignees(
                nodes,
                mission_metadata=dict(mission.get("metadata") or {}),
            )
            edges = [
                edge for edge in (
                    self.team_mission_rows.edge_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_edges WHERE mission_id = ? ORDER BY created_at ASC, edge_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if edge is not None
            ]
            run_bindings = [
                binding for binding in (
                    self.team_mission_rows.run_binding_from_row(row)
                    for row in self._conn.execute(
                        "SELECT * FROM team_mission_run_bindings WHERE mission_id = ? ORDER BY created_at ASC, run_id ASC",
                        (mission_id,),
                    ).fetchall()
                ) if binding is not None
            ]
            nodes = self.team_mission_rows.nodes_with_runtime_bindings(nodes, run_bindings)
            deliverables = [
                deliverable for deliverable in (
                    self.team_mission_rows.deliverable_from_row(row)
                    for row in self._conn.execute(
                        """
                        SELECT *
                        FROM team_mission_deliverables
                        WHERE mission_id = ?
                        ORDER BY updated_at ASC, created_at ASC, deliverable_id ASC
                        """,
                        (mission_id,),
                    ).fetchall()
                ) if deliverable
            ]
            result = self.team_mission_rows.result_from_row(self._conn.execute(
                "SELECT * FROM team_mission_results WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())
        latest_deliverable_by_node: Dict[str, Dict[str, Any]] = {}
        for deliverable in deliverables:
            node_id = _text(deliverable.get("node_id") or deliverable.get("nodeId"))
            if node_id:
                latest_deliverable_by_node[node_id] = deliverable
        nodes = [
            {
                **node,
                **({
                    "deliverable": latest_deliverable_by_node[_text(node.get("node_id"))],
                    "last_deliverable": latest_deliverable_by_node[_text(node.get("node_id"))],
                    "lastDeliverable": latest_deliverable_by_node[_text(node.get("node_id"))],
                } if _text(node.get("node_id")) in latest_deliverable_by_node else {}),
            }
            for node in nodes
        ]
        return {
            "mission": mission,
            "conversation": conversation or {},
            "nodes": nodes,
            "edges": edges,
            "run_bindings": run_bindings,
            "deliverables": deliverables,
            "result": result,
        }

    def get_team_mission_conversation_graph(self, conversation_id: str) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        with self._lock:
            conversation = self.team_mission_rows.conversation_from_row(self._conn.execute(
                f"""
                SELECT team_mission_conversations.*,
                       {self._PROJECTED_ACTIVE_MISSION_ID_SQL}
                FROM team_mission_conversations
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone())
            if conversation is None:
                return {}
            missions = [
                mission for mission in (
                    self.team_mission_rows.mission_from_row(row)
                    for row in self._conn.execute(
                        """
                        SELECT *
                        FROM team_missions
                        WHERE conversation_id = ?
                        ORDER BY created_at ASC, updated_at ASC, mission_id ASC
                        """,
                        (conversation_id,),
                    ).fetchall()
                ) if mission is not None
                # Hide the member-chat container: it is an internal runtime vehicle,
                # never a task the user should see on the canvas.
                and not bool((mission.get("metadata") or {}).get("member_chat_only"))
            ]
        message_page = _conversation_message_page(self, conversation, limit=100)
        if not missions:
            deliverable_projection = self.team_mission_conversation_deliverables.project(
                conversation,
                [],
            )
            return {
                "mission": {},
                "conversation": conversation,
                "nodes": [],
                "edges": [],
                "run_bindings": [],
                "task_frames": [],
                "runs": [],
                "last_message": deliverable_projection.get("last_message") or {},
                "last_message_preview": deliverable_projection.get("last_message_preview") or "",
                "last_message_at": deliverable_projection.get("last_message_at") or 0,
                "final_deliverables": [],
                "artifact_refs": [],
                "recent_messages": list(message_page.get("messages") or []),
                "recentMessages": list(message_page.get("messages") or []),
                "message_page_info": message_page.get("pageInfo") or {},
                "messagePageInfo": message_page.get("pageInfo") or {},
            }

        active_mission_id = _text(conversation.get("active_mission_id"))
        latest_mission = missions[-1]
        active_mission = next(
            (mission for mission in missions if _text(mission.get("mission_id")) == active_mission_id),
            latest_mission,
        )
        deliverable_projection = self.team_mission_conversation_deliverables.project(
            conversation,
            missions,
        )
        deliverables_by_mission = deliverable_projection.get("final_deliverables_by_mission") or {}
        deliverables_by_task = deliverable_projection.get("final_deliverables_by_task") or {}
        artifact_refs_by_mission = deliverable_projection.get("artifact_refs_by_mission") or {}
        artifact_refs_by_task = deliverable_projection.get("artifact_refs_by_task") or {}
        aggregate_nodes: List[Dict[str, Any]] = []
        aggregate_edges: List[Dict[str, Any]] = []
        aggregate_bindings: List[Dict[str, Any]] = []
        task_frames: List[Dict[str, Any]] = []
        runs: List[Dict[str, Any]] = []

        for mission in missions:
            mission_id = _text(mission.get("mission_id"))
            single_graph = self.get_team_mission_graph(mission_id)
            nodes = list(single_graph.get("nodes") or [])
            edges = list(single_graph.get("edges") or [])
            bindings = list(single_graph.get("run_bindings") or [])
            node_ids: List[str] = []
            root_node_id = ""
            for node in nodes:
                original_node_id = _text(node.get("node_id"))
                if not original_node_id:
                    continue
                namespaced_node_id = _conversation_graph_node_id(mission_id, original_node_id)
                metadata = dict(node.get("metadata") or {})
                metadata.setdefault("hermes_mission_id", mission_id)
                metadata.setdefault("hermes_node_id", original_node_id)
                metadata.setdefault("task_id", _task_id_from_mission(mission))
                projected_node = {
                    **node,
                    "node_id": namespaced_node_id,
                    "metadata": metadata,
                }
                aggregate_nodes.append(projected_node)
                node_ids.append(namespaced_node_id)
                if not root_node_id and _normalize_node_kind(_text(node.get("kind"))) == "root":
                    root_node_id = namespaced_node_id
            for edge in edges:
                from_node_id = _text(edge.get("from_node_id"))
                to_node_id = _text(edge.get("to_node_id"))
                if not from_node_id or not to_node_id:
                    continue
                metadata = dict(edge.get("metadata") or {})
                metadata.setdefault("hermes_mission_id", mission_id)
                aggregate_edges.append({
                    **edge,
                    "from_node_id": _conversation_graph_node_id(mission_id, from_node_id),
                    "to_node_id": _conversation_graph_node_id(mission_id, to_node_id),
                    "metadata": metadata,
                })
            for binding in bindings:
                original_node_id = _text(binding.get("node_id"))
                metadata = dict(binding.get("metadata") or {})
                metadata.setdefault("hermes_mission_id", mission_id)
                metadata.setdefault("hermes_node_id", original_node_id)
                aggregate_bindings.append({
                    **binding,
                    "node_id": _conversation_graph_node_id(mission_id, original_node_id) if original_node_id else "",
                    "metadata": metadata,
                })
            task_id = _task_id_from_mission(mission)
            frame_id = f"mission-frame:{mission_id}"
            frame_artifact_refs = _dedupe_artifact_refs([
                *list(artifact_refs_by_mission.get(mission_id, [])),
                *list(artifact_refs_by_task.get((mission_id, task_id), [])),
            ])
            final_deliverable = _final_deliverable_for_frame(
                deliverables_by_mission,
                deliverables_by_task,
                mission_id,
                task_id,
                frame_artifact_refs,
            )
            frame = {
                "id": frame_id,
                "runId": _text(mission.get("leader_session_id")),
                "missionId": mission_id,
                "mission_id": mission_id,
                "taskId": task_id,
                "task_id": task_id,
                "title": _text(mission.get("title")) or _text(conversation.get("title")) or "团队任务",
                "objective": _text(mission.get("objective")) or _text(mission.get("title")) or "团队任务",
                "status": _text(mission.get("status")) or "planning",
                "source": "hermes_conversation",
                "rootNodeId": root_node_id or (node_ids[0] if node_ids else ""),
                "root_node_id": root_node_id or (node_ids[0] if node_ids else ""),
                "nodeIds": node_ids,
                "node_ids": node_ids,
                "createdAt": mission.get("created_at") or 0,
                "created_at": mission.get("created_at") or 0,
                "updatedAt": mission.get("updated_at") or 0,
                "updated_at": mission.get("updated_at") or 0,
                "completedAt": mission.get("completed_at"),
                "completed_at": mission.get("completed_at"),
                "artifactRefs": frame_artifact_refs,
                "artifact_refs": frame_artifact_refs,
            }
            if final_deliverable:
                frame.update({
                    "finalDeliverable": final_deliverable,
                    "final_deliverable": final_deliverable,
                    "deliverableMessageId": final_deliverable.get("messageId") or "",
                    "deliverable_message_id": final_deliverable.get("message_id") or "",
                })
            task_frames.append(frame)
            runs.append({
                "id": frame_id,
                "conversationId": conversation_id,
                "conversation_id": conversation_id,
                **frame,
            })

        return {
            "mission": active_mission,
            "conversation": conversation,
            "nodes": aggregate_nodes,
            "edges": aggregate_edges,
            "run_bindings": aggregate_bindings,
            "task_frames": task_frames,
            "runs": runs,
            "last_message": deliverable_projection.get("last_message") or {},
            "last_message_preview": deliverable_projection.get("last_message_preview") or "",
            "last_message_at": deliverable_projection.get("last_message_at") or 0,
            "final_deliverables": list(deliverable_projection.get("final_deliverables") or []),
            "artifact_refs": list(deliverable_projection.get("artifact_refs") or []),
            "recent_messages": list(message_page.get("messages") or []),
            "recentMessages": list(message_page.get("messages") or []),
            "message_page_info": message_page.get("pageInfo") or {},
            "messagePageInfo": message_page.get("pageInfo") or {},
        }
