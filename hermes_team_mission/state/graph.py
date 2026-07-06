from __future__ import annotations

import re
from typing import Any, Dict, List

from hermes_team_mission.domain.handoff_contract import deliverable_is_effective_handoff
from hermes_team_mission.domain.handoff_contract import node_requires_authoritative_handoff
from hermes_team_mission.domain.node_kinds import TEAM_MISSION_CONTROL_NODE_KINDS
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind
from hermes_team_mission.domain.statuses import is_cancelled_mission_status
from hermes_team_mission.domain.statuses import is_terminal_mission_status


_DEPENDENCY_EDGE_KINDS = {"depends_on", "dependency", "blocks", "delegates"}
_DEPENDENCY_SATISFIED_STATUSES = {"completed", "verified"}
_STARTABLE_NODE_STATUSES = {"ready"}
_WAITING_DEPENDENCY_STATUSES = {"todo", "waiting_dependency", "blocked_waiting_dependency"}
_ACTIVE_NODE_STATUSES = {"running", "starting", "waiting_approval"}
_EXECUTION_MODES_REQUIRE_FINALIZERS = {"supervised_mission", "autonomous_mission", "manual_graph"}
_NON_WORK_NODE_KINDS = TEAM_MISSION_CONTROL_NODE_KINDS


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _mission_allows_auto_finalizers(mission: Dict[str, Any] | None) -> bool:
    mission = mission if isinstance(mission, dict) else {}
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    return any(
        _truthy(metadata.get(key))
        for key in (
            "allow_auto_finalizers",
            "allowAutoFinalizers",
            "legacy_auto_finalizers",
            "legacyAutoFinalizers",
        )
    )


def _node_handoff_deliverable(node: Dict[str, Any] | None) -> Dict[str, Any]:
    node = node if isinstance(node, dict) else {}
    for key in ("deliverable", "last_deliverable", "lastDeliverable"):
        value = node.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _node_satisfies_dependency(node: Dict[str, Any] | None) -> bool:
    node = node if isinstance(node, dict) else {}
    if str(node.get("status") or "") not in _DEPENDENCY_SATISFIED_STATUSES:
        return False
    if not node_requires_authoritative_handoff(node):
        return True
    return deliverable_is_effective_handoff(_node_handoff_deliverable(node))


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
    return bool(required_nodes) and all(_node_satisfies_dependency(node) for node in required_nodes)


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
            _node_satisfies_dependency(nodes_by_id.get(dep) or {})
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
    finalizer_changes = []
    if _mission_allows_auto_finalizers(mission):
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
    prior_mission_status = str(mission.get("status") or "")
    # BUG FIX(2026-07-06): 一旦 mission 已进入终态(cancelled/failed/completed/blocked/
    # interrupted),必须冻结 mission_status,不再让节点级 reduce 反过来覆盖它。
    #
    # 症状:用户在 plan 审批阶段点"取消" → reject_team_mission_plan 把 mission 和
    # approval_gate 节点都写成 cancelled;紧接着 leader 起 report run 汇报"任务已
    # 取消",report run 完成时 session_events 会再次触发 reduce_team_mission_graph
    # (session_events.py:180)。旧逻辑走到下面 approval_pending 判定时:approval_gate
    # 节点状态是 "cancelled",而 _DEPENDENCY_SATISFIED_STATUSES = {"completed",
    # "verified"} 不含 cancelled → approval_pending=True → mission_status 被推导
    # 回 "waiting_approval" → upsert_team_mission 把已 cancelled 的 mission 静默
    # 覆盖回 waiting_approval → 前端下一次 render 立即看到审批卡再显,用户被迫再
    # 点一次取消。用户描述"leader 报告响应结束瞬间弹审批"完全对应这条时序。
    #
    # 修法(核心层 A):任何终态 mission 都不再推导 status。剩下的下游动作
    # (session_index projection / terminal 事件 append)仍照常执行 —— 这些是
    # idempotent 的,重跑不会造成额外副作用。
    if is_terminal_mission_status(prior_mission_status):
        mission_status = prior_mission_status
    else:
        approval_pending = (
            mode == "supervised_mission"
            and any(
                normalize_team_mission_node_kind(node.get("kind")) == "approval_gate"
                # BUG FIX(2026-07-06)防御层 B:approval_gate 节点自身进入终态
                # (cancelled/failed 等)也算 "不 pending",避免 non-terminal
                # mission 因残留的终态 approval_gate 节点被永久锁在 waiting_approval。
                and str(node.get("status") or "") not in _DEPENDENCY_SATISFIED_STATUSES
                and str(node.get("status") or "") not in {"cancelled", "canceled", "failed"}
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
    if is_terminal_mission_status(mission_status):
        finalized_result: Dict[str, Any] = {}
        try:
            from hermes_team_mission.runtime.mission_result import finalize_team_mission_result

            finalized_result = finalize_team_mission_result(db, mission_id)
        except Exception:
            pass
        try:
            from hermes_team_mission.runtime.snapshot_events import append_team_mission_snapshot_updated

            append_team_mission_snapshot_updated(
                db,
                mission_id=mission_id,
                reason="mission_terminal",
                status=mission_status,
                result_id=str((finalized_result or {}).get("result_id") or ""),
            )
        except Exception:
            pass
        linked_status = "cancelled" if is_cancelled_mission_status(mission_status) else mission_status
        linker = getattr(db, "_set_linked_conversation_mission_status", None)
        if callable(linker):
            try:
                linker(mission_id=mission_id, mission=mission, status=linked_status)
            except Exception:
                pass
    # Project the mission's live state onto its conversation's session_index row
    # so the sidebar's running/approval indicator stays correct from the
    # single-query read (no read-time mission-graph walk).
    #
    # MUST run on EVERY reduce, not only when prior!=new mission_status above.
    # Strategy actions (e.g. complete_team_mission_plan) upsert the new mission
    # status BEFORE calling reduce, so by the time reduce reads `mission` from
    # the graph the prior_mission_status already equals the freshly-computed
    # mission_status — the change-guarded path skips, and session_index keeps
    # the stale projection from whatever leader-running phase wrote last.
    # That's exactly the bug where session_index.waiting_approval never flips
    # to 1 while the plan-approval gate is open, so the sidebar shows running
    # spinner instead of the approval indicator. Projecting on every reduce is
    # an idempotent single-row UPDATE, well below reduce's overall cost.
    index_updater = getattr(db, "update_session_index_for_mission", None)
    if callable(index_updater):
        ms = mission_status.lower()
        if is_terminal_mission_status(ms):
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
    # accumulate unbounded across never-revisited missions. Gated on the
    # actual prior!=new transition so it still fires once per mission.
    if (
        is_terminal_mission_status(mission_status)
        and not is_terminal_mission_status(prior_mission_status)
    ):
        pruner = getattr(db, "prune_team_mission_events", None)
        if callable(pruner):
            try:
                pruner(mission_id)
            except Exception:
                pass
    if changed_nodes or mission_status != prior_mission_status:
        try:
            from hermes_team_mission.runtime.snapshot_events import append_team_mission_snapshot_updated

            append_team_mission_snapshot_updated(
                db,
                mission_id=mission_id,
                reason="graph_reduced",
                status=mission_status,
            )
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
    if not _mission_allows_auto_finalizers(mission):
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
    if not all(_node_satisfies_dependency(node) for node in work_nodes):
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
                "delivery_channel": "handoff",
                "requires_explicit_handoff": True,
                "requires_process_events": True,
                "requires_deliverable": True,
            },
            metadata=_metadata_with_task_id(
                {"mode": mode, "phase": "verifying", "auto_finalizer": True, "system_generated": True},
                active_task_id,
            ),
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
    verifier_terminal = all(_node_satisfies_dependency(node) for node in verifier_nodes)
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
                "delivery_channel": "handoff",
                "requires_explicit_handoff": True,
                "requires_process_events": True,
                "requires_deliverable": True,
            },
            metadata=_metadata_with_task_id(
                {"mode": mode, "phase": "synthesis", "auto_finalizer": True, "system_generated": True},
                active_task_id,
            ),
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
