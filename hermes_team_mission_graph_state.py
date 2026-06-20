from __future__ import annotations

import re
from typing import Any, Dict, List

from hermes_team_mission_node_kinds import TEAM_MISSION_CONTROL_NODE_KINDS
from hermes_team_mission_node_kinds import normalize_team_mission_node_kind


_DEPENDENCY_EDGE_KINDS = {"depends_on", "dependency", "blocks", "delegates"}
_DEPENDENCY_SATISFIED_STATUSES = {"completed", "verified"}
_STARTABLE_NODE_STATUSES = {"ready"}
_WAITING_DEPENDENCY_STATUSES = {"todo", "waiting_dependency", "blocked_waiting_dependency"}
_ACTIVE_NODE_STATUSES = {"running", "starting", "waiting_approval"}
_TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
_EXECUTION_MODES_REQUIRE_FINALIZERS = {"supervised_mission", "autonomous_mission", "manual_graph"}
_NON_WORK_NODE_KINDS = TEAM_MISSION_CONTROL_NODE_KINDS


def _task_id_from_metadata(metadata: Dict[str, Any] | None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), dict) else {}
    return str(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
        or ""
    ).strip()


def _node_matches_task(node: Dict[str, Any] | None, task_id: str) -> bool:
    if not task_id:
        return True
    metadata = node.get("metadata") if isinstance(node, dict) and isinstance(node.get("metadata"), dict) else {}
    node_task_id = _task_id_from_metadata(metadata)
    if node_task_id:
        return node_task_id == task_id
    node_id = str((node or {}).get("node_id") or "")
    return task_id in node_id


def _task_scoped_node_id(mission_id: str, task_id: str, suffix: str) -> str:
    if task_id:
        safe_task = re.sub(r"[^A-Za-z0-9_.:-]+", "-", task_id).strip("-")[:64] or "task"
        return f"team-mission:{mission_id}:{safe_task}:{suffix}"
    return f"team-mission:{mission_id}:{suffix}"


def _metadata_with_task_id(metadata: Dict[str, Any] | None, task_id: str) -> Dict[str, Any]:
    result = dict(metadata or {})
    if task_id:
        result["task_id"] = task_id
    return result


def _append_structural_event(db: Any, mission_id: str, event: Dict[str, Any]) -> None:
    append = getattr(db, "append_team_mission_structural_event", None)
    if not callable(append):
        return
    append(mission_id=mission_id, source_event=event)


def _execution_mode_requires_finalizers(mode: str) -> bool:
    return mode in _EXECUTION_MODES_REQUIRE_FINALIZERS


def _execution_scope_nodes(nodes: List[Dict[str, Any]], task_id: str) -> List[Dict[str, Any]]:
    return [node for node in nodes if _node_matches_task(node, task_id)]


def _required_execution_completed(
    *,
    mode: str,
    nodes: List[Dict[str, Any]],
    task_id: str,
) -> bool:
    if not _execution_mode_requires_finalizers(mode):
        return True
    scoped_nodes = _execution_scope_nodes(nodes, task_id)
    work_nodes = [
        node for node in scoped_nodes
        if normalize_team_mission_node_kind(node.get("kind")) not in _NON_WORK_NODE_KINDS
    ]
    synthesis_nodes = [
        node for node in scoped_nodes
        if normalize_team_mission_node_kind(node.get("kind")) == "synthesis"
    ]
    if not work_nodes or not synthesis_nodes:
        return False
    required_nodes = [
        node for node in scoped_nodes
        if normalize_team_mission_node_kind(node.get("kind")) not in {"root", "approval_gate"}
    ]
    return bool(required_nodes) and all(
        str(node.get("status") or "") in _DEPENDENCY_SATISFIED_STATUSES
        for node in required_nodes
    )


def reduce_team_mission_graph(db: Any, mission_id: str) -> Dict[str, Any]:
    mission_id = str(mission_id or "").strip()
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else None
    if not isinstance(mission, dict):
        return {}
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
    edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]
    nodes_by_id = {str(node.get("node_id") or ""): node for node in nodes}
    dependency_sources_by_target: Dict[str, set[str]] = {}
    for edge in edges:
        kind = str(edge.get("kind") or "depends_on")
        if kind not in _DEPENDENCY_EDGE_KINDS:
            continue
        source = str(edge.get("from_node_id") or "")
        target = str(edge.get("to_node_id") or "")
        if source and target:
            dependency_sources_by_target.setdefault(target, set()).add(source)
    changed_nodes: List[Dict[str, Any]] = []
    ready_node_ids: List[str] = []
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        status = str(node.get("status") or "")
        if status in {"completed", "verified", "failed", "cancelled", "canceled"}:
            continue
        dependencies = dependency_sources_by_target.get(node_id, set())
        dependencies_satisfied = all(
            str((nodes_by_id.get(dep) or {}).get("status") or "") in _DEPENDENCY_SATISFIED_STATUSES
            for dep in dependencies
        )
        next_status = status
        if dependencies and not dependencies_satisfied and status in {"ready", "todo", "waiting_dependency"}:
            next_status = "blocked_waiting_dependency"
        elif dependencies_satisfied and status in _WAITING_DEPENDENCY_STATUSES:
            next_status = "ready"
        elif not dependencies and status in _WAITING_DEPENDENCY_STATUSES:
            next_status = "ready"
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        if next_status != status:
            node = db.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=node_id,
                kind=normalize_team_mission_node_kind(node.get("kind")),
                title=str(node.get("title") or ""),
                objective=str(node.get("objective") or ""),
                status=next_status,
                assignee_profile_id=str(node.get("assignee_profile_id") or ""),
                assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
                runtime_scope_key=str(node.get("runtime_scope_key") or ""),
                output_contract=dict(node.get("output_contract") or {}),
                metadata={
                    **metadata,
                    "dependency_count": len(dependencies),
                    "dependencies_satisfied": dependencies_satisfied,
                },
                position_x=float(node.get("position_x") or 0),
                position_y=float(node.get("position_y") or 0),
            )
            changed_nodes.append(node)
        if next_status in _STARTABLE_NODE_STATUSES and not bool(metadata.get("manual_start")):
            ready_node_ids.append(node_id)
    finalizer_changes = ensure_team_mission_finalizers(
        db,
        mission=mission,
        nodes=[node for node in db.get_team_mission_graph(mission_id).get("nodes", []) if isinstance(node, dict)],
        edges=edges,
    )
    if finalizer_changes:
        changed_nodes.extend(finalizer_changes)
        graph_after_finalizers = db.get_team_mission_graph(mission_id)
        ready_node_ids = [
            str(node.get("node_id") or "")
            for node in graph_after_finalizers.get("nodes", [])
            if isinstance(node, dict)
            and str(node.get("status") or "") in _STARTABLE_NODE_STATUSES
            and not bool((node.get("metadata") or {}).get("manual_start"))
        ]
    updated_graph = db.get_team_mission_graph(mission_id)
    updated_nodes = [node for node in updated_graph.get("nodes", []) if isinstance(node, dict)]
    statuses = {str(node.get("status") or "") for node in updated_nodes}
    mode = str(mission.get("mode") or "")
    mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    active_task_id = _task_id_from_metadata(mission_metadata)
    completion_satisfied = bool(updated_nodes) and statuses <= _DEPENDENCY_SATISFIED_STATUSES
    required_execution_completed = _required_execution_completed(
        mode=mode,
        nodes=updated_nodes,
        task_id=active_task_id,
    )
    completion_blocked_by_required_execution = (
        completion_satisfied
        and _execution_mode_requires_finalizers(mode)
        and not required_execution_completed
    )
    approval_pending = (
        mode == "supervised_mission"
        and any(
            normalize_team_mission_node_kind(node.get("kind")) == "approval_gate"
            and str(node.get("status") or "") not in _DEPENDENCY_SATISFIED_STATUSES
            for node in updated_nodes
        )
    )
    active = bool(statuses & _ACTIVE_NODE_STATUSES)
    if approval_pending:
        mission_status = "waiting_approval"
    elif completion_satisfied and required_execution_completed:
        mission_status = "completed"
    elif "failed" in statuses:
        mission_status = "failed"
    elif active:
        mission_status = "running"
    elif ready_node_ids:
        mission_status = "ready"
    elif completion_blocked_by_required_execution:
        mission_status = "running"
    elif "blocked" in statuses:
        mission_status = "blocked"
    elif "blocked_waiting_dependency" in statuses:
        mission_status = "waiting_dependency"
    else:
        mission_status = str(mission.get("status") or "draft")
    prior_mission_status = str(mission.get("status") or "")
    if mission_status != prior_mission_status:
        db.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status=mission_status,
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata=dict(mission.get("metadata") or {}),
        )
        updated_graph = db.get_team_mission_graph(mission_id)
        # Project the mission's live state onto its conversation's session_index
        # row so the sidebar's running/approval indicator stays correct from the
        # single-query read (no read-time mission-graph walk). Only on status
        # change, so this is low frequency.
        index_updater = getattr(db, "update_session_index_for_mission", None)
        if callable(index_updater):
            ms = mission_status.lower()
            if ms in _TERMINAL_MISSION_STATUSES:
                idx_status, idx_running, idx_waiting = "idle", False, False
            elif ms == "waiting_approval":
                idx_status, idx_running, idx_waiting = "waiting_approval", False, True
            else:
                idx_status, idx_running, idx_waiting = "running", True, False
            try:
                index_updater(
                    mission_id,
                    status=idx_status,
                    running=idx_running,
                    waiting_approval=idx_waiting,
                )
            except Exception:
                pass
        # Cap canonical-log growth at the source: the instant a mission first
        # reaches a terminal state, prune its high-volume stream deltas. This
        # runs once per mission at completion regardless of whether the
        # conversation is ever reopened, so team_mission_events cannot
        # accumulate unbounded across never-revisited missions.
        if (
            mission_status.lower() in _TERMINAL_MISSION_STATUSES
            and prior_mission_status.lower() not in _TERMINAL_MISSION_STATUSES
        ):
            pruner = getattr(db, "prune_team_mission_events", None)
            if callable(pruner):
                try:
                    pruner(mission_id)
                except Exception:
                    pass
    return {
        "mission_id": mission_id,
        "graph": updated_graph,
        "changed_nodes": changed_nodes,
        "ready_node_ids": ready_node_ids,
        "mission_status": mission_status,
    }


def ensure_team_mission_finalizers(
    db: Any,
    *,
    mission: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    mission_id = str((mission or {}).get("mission_id") or "").strip()
    mode = str((mission or {}).get("mode") or "").strip()
    if not mission_id or mode not in _EXECUTION_MODES_REQUIRE_FINALIZERS:
        return []
    mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    active_task_id = _task_id_from_metadata(mission_metadata)
    nodes_by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        if active_task_id and not _node_matches_task(node, active_task_id):
            continue
        nodes_by_kind.setdefault(normalize_team_mission_node_kind(node.get("kind")), []).append(node)
    work_nodes = [
        node for node in nodes
        if normalize_team_mission_node_kind(node.get("kind")) not in _NON_WORK_NODE_KINDS
        and _node_matches_task(node, active_task_id)
    ]
    if not work_nodes:
        return []
    if not all(str(node.get("status") or "") in _DEPENDENCY_SATISFIED_STATUSES for node in work_nodes):
        return []
    changed: List[Dict[str, Any]] = []
    verifier_nodes = nodes_by_kind.get("verifier") or []
    synthesis_nodes = nodes_by_kind.get("synthesis") or []
    if not verifier_nodes:
        verifier_id = _task_scoped_node_id(mission_id, active_task_id, "verifier")
        verifier = db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id=verifier_id,
            kind="verifier",
            title="验收执行结果",
            objective="检查所有执行节点是否满足任务目标和交付要求。",
            status="ready",
            runtime_scope_key=f"team:{mission_id}:{active_task_id + ':' if active_task_id else ''}verifier",
            output_contract={
                "format": "verification_report",
                "requires_process_events": True,
                "requires_deliverable": True,
            },
            metadata=_metadata_with_task_id({"mode": mode, "phase": "verifying", "auto_finalizer": True}, active_task_id),
            position_x=0,
            position_y=720,
        )
        changed.append(verifier)
        _append_structural_event(
            db,
            mission_id,
            {"type": "mission.node.created", "payload": {"node": verifier}},
        )
        existing_pairs = {
            (str(edge.get("from_node_id") or ""), str(edge.get("to_node_id") or ""))
            for edge in edges
        }
        for node in work_nodes:
            node_id = str(node.get("node_id") or "")
            if node_id and (node_id, verifier_id) not in existing_pairs:
                edge = db.upsert_team_mission_edge(
                    mission_id=mission_id,
                    from_node_id=node_id,
                    to_node_id=verifier_id,
                    kind="depends_on",
                    metadata=_metadata_with_task_id({"auto_finalizer": True}, active_task_id),
                )
                _append_structural_event(
                    db,
                    mission_id,
                    {"type": "mission.edge.created", "payload": {"edge": edge}},
                )
        return changed
    verifier_terminal = all(
        str(node.get("status") or "") in _DEPENDENCY_SATISFIED_STATUSES
        for node in verifier_nodes
    )
    if verifier_terminal and not synthesis_nodes:
        synthesis_id = _task_scoped_node_id(mission_id, active_task_id, "synthesis")
        synthesis = db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id=synthesis_id,
            kind="synthesis",
            title="汇总最终交付",
            objective="整合执行结果、验收结论和最终交付内容。",
            status="ready",
            runtime_scope_key=f"team:{mission_id}:{active_task_id + ':' if active_task_id else ''}synthesis",
            output_contract={
                "format": "final_deliverable",
                "requires_process_events": True,
                "requires_deliverable": True,
            },
            metadata=_metadata_with_task_id({"mode": mode, "phase": "synthesis", "auto_finalizer": True}, active_task_id),
            position_x=0,
            position_y=960,
        )
        changed.append(synthesis)
        _append_structural_event(
            db,
            mission_id,
            {"type": "mission.node.created", "payload": {"node": synthesis}},
        )
        for verifier in verifier_nodes:
            verifier_id = str(verifier.get("node_id") or "")
            if verifier_id:
                edge = db.upsert_team_mission_edge(
                    mission_id=mission_id,
                    from_node_id=verifier_id,
                    to_node_id=synthesis_id,
                    kind="depends_on",
                    metadata=_metadata_with_task_id({"auto_finalizer": True}, active_task_id),
                )
                _append_structural_event(
                    db,
                    mission_id,
                    {"type": "mission.edge.created", "payload": {"edge": edge}},
                )
    return changed
