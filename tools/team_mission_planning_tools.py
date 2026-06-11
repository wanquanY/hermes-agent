"""Internal Hermes Team Mission planning tools.

These tools are intentionally available only through the internal
``team_mission_planning`` toolset.  They let a Team Mission Leader run mutate
the native Hermes mission graph during the planning/change-request phase while
keeping all authorization anchored to the run binding persisted by Hermes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hermes_state import SessionDB
from hermes_team_mission_assignees import normalized_member_dicts
from hermes_team_mission_modes import strategy_for_mode
from hermes_team_mission_node_kinds import metadata_with_normalized_node_kind
from hermes_team_mission_node_kinds import normalize_team_mission_node_kind
from tools.registry import registry, tool_error, tool_result


_TOOLSET = "team_mission_planning"
_LEADER_ROLES = {"leader", "root"}
_LEADER_NODE_KINDS = {"root"}
_MUTATION_PHASES = {"planning", "change_request"}
_RESERVED_NODE_KINDS = {"root", "approval_gate"}
_RUNNING_STATUSES = {"running", "starting", "completed", "verified", "failed", "cancelled", "interrupted"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_db(parent_agent=None):
    db = getattr(parent_agent, "_session_db", None) if parent_agent is not None else None
    if db is not None:
        return db
    try:
        from tui_gateway import server

        return server._get_db()
    except Exception:
        return SessionDB()


def _active_run_id(args: dict[str, Any], parent_agent=None) -> str:
    for key in ("run_id", "runId"):
        value = _text(args.get(key))
        if value:
            return value
    for attr in (
        "_hermes_active_run_id",
        "_active_run_id",
        "active_run_id",
        "run_id",
    ):
        value = _text(getattr(parent_agent, attr, ""))
        if value:
            return value
    return ""


def _graph_summary(graph: dict[str, Any]) -> dict[str, Any]:
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    edges = graph.get("edges") if isinstance(graph, dict) else []
    return {
        "mission_id": _text((mission or {}).get("mission_id")),
        "mission_status": _text((mission or {}).get("status")),
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
        "edge_count": len(edges) if isinstance(edges, list) else 0,
    }


def _task_id_from_context(binding: Mapping[str, Any], node: Mapping[str, Any]) -> str:
    node_metadata = _metadata(node.get("metadata"))
    binding_metadata = _metadata(binding.get("metadata"))
    return _text(
        node_metadata.get("task_id")
        or node_metadata.get("taskId")
        or node_metadata.get("submitted_task_id")
        or node_metadata.get("submittedTaskId")
        or binding_metadata.get("task_id")
        or binding_metadata.get("taskId")
        or binding_metadata.get("submitted_task_id")
        or binding_metadata.get("submittedTaskId")
    )


def _task_text_from_context(binding: Mapping[str, Any], node: Mapping[str, Any], *keys: str) -> str:
    node_metadata = _metadata(node.get("metadata"))
    binding_metadata = _metadata(binding.get("metadata"))
    for key in keys:
        value = _text(node_metadata.get(key) or binding_metadata.get(key))
        if value:
            return value
    return ""


def _metadata_with_task_context(
    metadata: Mapping[str, Any],
    *,
    run_id: str,
    task_id: str,
    task_title: str = "",
    task_objective: str = "",
) -> dict[str, Any]:
    result = {**dict(metadata), "planned_by_run_id": run_id}
    if task_id:
        result.setdefault("task_id", task_id)
    if task_title:
        result.setdefault("task_title", task_title)
    if task_objective:
        result.setdefault("task_objective", task_objective)
    return result


def _authorized_context(args: dict[str, Any], parent_agent=None) -> tuple[Any, str, dict[str, Any], dict[str, Any], dict[str, Any], Any, dict[str, Any]] | str:
    db = _get_db(parent_agent)
    if db is None:
        return "Session database is not available."
    run_id = _active_run_id(args, parent_agent)
    if not run_id:
        return "run_id is required for Team Mission planning tools."
    binding = db.get_team_mission_run_binding(run_id)
    if not binding:
        return "Current run is not bound to a Team Mission node."
    mission_id = _text(binding.get("mission_id"))
    node_id = _text(binding.get("node_id"))
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return "Bound Team Mission was not found."
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return "Bound Team Mission node was not found."
    node_metadata = _metadata(node.get("metadata"))
    binding_role = _text(binding.get("role"))
    actor = _text(node_metadata.get("role") or binding_role or node.get("kind"))
    node_kind = _text(node.get("kind"))
    phase = _text(node_metadata.get("phase") or mission.get("status"))
    if binding_role not in _LEADER_ROLES and actor not in _LEADER_ROLES and node_kind not in _LEADER_NODE_KINDS:
        return "Only a Team Mission Leader run can mutate the mission graph."
    if phase not in _MUTATION_PHASES:
        return f"Team Mission graph mutation is not allowed during phase '{phase or 'unknown'}'."
    strategy = strategy_for_mode(_text(mission.get("mode")) or "supervised_mission")
    if not strategy.can_mutate_graph(actor="leader", phase=phase):
        return f"Mission mode '{strategy.mode}' does not allow Leader graph mutation during phase '{phase}'."
    return db, run_id, binding, mission, graph, strategy, node


def _normalize_node_id(value: Any) -> str:
    node_id = _text(value)
    return node_id


def _mission_members(mission: Mapping[str, Any]) -> list[dict[str, Any]]:
    mission_metadata = _metadata(mission.get("metadata"))
    raw_members = mission_metadata.get("members")
    return normalized_member_dicts(raw_members if isinstance(raw_members, list) else [])


def _member_ids_hint(members: list[dict[str, Any]]) -> str:
    if not members:
        return "none"
    return ", ".join(
        f"{_text(member.get('member_id'))}({_text(member.get('role')) or 'member'})"
        for member in members
        if _text(member.get("member_id"))
    ) or "none"


def _leader_member_id(members: list[dict[str, Any]]) -> str:
    for member in members:
        if _text(member.get("role")) in {"leader", "lead", "root"}:
            return _text(member.get("member_id"))
    return _text((members[0] if members else {}).get("member_id"))


def _member_by_id(members: list[dict[str, Any]], member_id: str) -> dict[str, Any]:
    normalized = _text(member_id)
    for member in members:
        if _text(member.get("member_id")) == normalized:
            return member
    return {}


def _member_by_profile(members: list[dict[str, Any]], profile_id: str, version_id: str) -> dict[str, Any]:
    normalized_profile = _text(profile_id)
    normalized_version = _text(version_id)
    for member in members:
        if _text(member.get("profile_id")) != normalized_profile:
            continue
        member_version = _text(member.get("profile_version_id"))
        if not normalized_version or not member_version or member_version == normalized_version:
            return member
    return {}


def _member_by_role(members: list[dict[str, Any]], role: str) -> dict[str, Any]:
    normalized = _text(role)
    for member in members:
        if _text(member.get("role")) == normalized:
            return member
    return {}


def _validate_assignee_reference(
    *,
    mission: Mapping[str, Any],
    assignee_member_id: str,
    assignee_profile_id: str,
    assignee_profile_version_id: str,
    assignee_role: str,
) -> str:
    members = _mission_members(mission)
    member_hint = _member_ids_hint(members)
    leader_hint = _leader_member_id(members)
    control_hint = (
        f"; for verifier/synthesis nodes omit assignee fields or use Leader member_id '{leader_hint}'"
        if leader_hint
        else "; for verifier/synthesis nodes omit assignee fields"
    )
    if assignee_member_id and not _member_by_id(members, assignee_member_id):
        return (
            f"assignee_member_id '{assignee_member_id}' is not a Team Mission member. "
            f"Allowed member ids: {member_hint}{control_hint}."
        )
    if assignee_profile_id and members and not _member_by_profile(members, assignee_profile_id, assignee_profile_version_id):
        return (
            f"assignee_profile_id '{assignee_profile_id}' does not belong to a Team Mission member. "
            f"Allowed member ids: {member_hint}{control_hint}."
        )
    if assignee_role and members and not _member_by_role(members, assignee_role):
        return (
            f"assignee_role '{assignee_role}' does not match a Team Mission member role. "
            f"Allowed member ids: {member_hint}{control_hint}."
        )
    return ""


def _handle_node_create(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    ctx = _authorized_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, run_id, binding, mission, _graph, _strategy, planning_node = ctx
    mission_id = _text(binding.get("mission_id"))
    task_id = _task_id_from_context(binding, planning_node)
    task_title = _task_text_from_context(binding, planning_node, "task_title", "taskTitle") or _text(planning_node.get("title"))
    task_objective = _task_text_from_context(binding, planning_node, "task_objective", "taskObjective") or _text(planning_node.get("objective"))
    node_id = _normalize_node_id(args.get("node_id") or args.get("nodeId") or args.get("id"))
    title = _text(args.get("title"))
    objective = _text(args.get("objective"))
    if not node_id:
        return tool_error("node_id is required.")
    if not title:
        return tool_error("title is required.")
    if not objective:
        return tool_error("objective is required.")
    raw_kind = _text(args.get("kind")) or "worker"
    kind = normalize_team_mission_node_kind(raw_kind)
    if kind in _RESERVED_NODE_KINDS:
        return tool_error(f"node kind '{kind}' is reserved for Hermes runtime.")
    status = _text(args.get("status")) or "ready"
    if status in _RUNNING_STATUSES:
        return tool_error("Planning tools can only create non-running nodes.")
    output_contract = args.get("output_contract") or args.get("outputContract") or {}
    metadata = args.get("metadata") or {}
    if not isinstance(output_contract, Mapping):
        return tool_error("output_contract must be an object.")
    if not isinstance(metadata, Mapping):
        return tool_error("metadata must be an object.")
    metadata = dict(metadata)
    metadata = metadata_with_normalized_node_kind(metadata, raw_kind=raw_kind, canonical_kind=kind)
    assignee_member_id = _text(
        args.get("assignee_member_id")
        or args.get("assigneeMemberId")
        or metadata.get("assignee_member_id")
        or metadata.get("assigneeMemberId")
        or metadata.get("member_id")
        or metadata.get("memberId")
    )
    assignee_profile_id = _text(args.get("assignee_profile_id") or args.get("assigneeProfileId"))
    assignee_profile_version_id = _text(args.get("assignee_profile_version_id") or args.get("assigneeProfileVersionId"))
    assignee_role = _text(args.get("assignee_role") or args.get("assigneeRole") or metadata.get("assignee_role") or metadata.get("assigneeRole"))
    assignee_error = _validate_assignee_reference(
        mission=mission,
        assignee_member_id=assignee_member_id,
        assignee_profile_id=assignee_profile_id,
        assignee_profile_version_id=assignee_profile_version_id,
        assignee_role=assignee_role,
    )
    if assignee_error:
        return tool_error(assignee_error)
    if assignee_member_id:
        metadata.setdefault("assignee_member_id", assignee_member_id)
    if assignee_role:
        metadata.setdefault("assignee_role", assignee_role)
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=kind,
        title=title,
        objective=objective,
        status=status,
        assignee_profile_id=assignee_profile_id,
        assignee_profile_version_id=assignee_profile_version_id,
        runtime_scope_key=_text(args.get("runtime_scope_key") or args.get("runtimeScopeKey")),
        output_contract=dict(output_contract),
        metadata=_metadata_with_task_context(
            metadata,
            run_id=run_id,
            task_id=task_id,
            task_title=task_title,
            task_objective=task_objective,
        ),
        position_x=_number(args.get("position_x") if "position_x" in args else args.get("x")),
        position_y=_number(args.get("position_y") if "position_y" in args else args.get("y")),
    )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=run_id,
        event={"type": "mission.node.created", "payload": {"node": node}},
    )
    reduced = db.reduce_team_mission_graph(mission_id)
    graph = reduced.get("graph") if isinstance(reduced, dict) and reduced.get("graph") else db.get_team_mission_graph(mission_id)
    return tool_result(
        success=True,
        mission_id=mission_id,
        node=node,
        graph_summary=_graph_summary(graph),
    )


def _handle_edge_create(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    ctx = _authorized_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, run_id, binding, mission, _graph, _strategy, planning_node = ctx
    mission_id = _text(binding.get("mission_id"))
    task_id = _task_id_from_context(binding, planning_node)
    task_title = _task_text_from_context(binding, planning_node, "task_title", "taskTitle") or _text(planning_node.get("title"))
    task_objective = _task_text_from_context(binding, planning_node, "task_objective", "taskObjective") or _text(planning_node.get("objective"))
    from_node_id = _text(args.get("from_node_id") or args.get("fromNodeId") or args.get("source"))
    to_node_id = _text(args.get("to_node_id") or args.get("toNodeId") or args.get("target"))
    if not from_node_id or not to_node_id:
        return tool_error("from_node_id and to_node_id are required.")
    if from_node_id == to_node_id:
        return tool_error("A Team Mission edge cannot target the same node.")
    if not db.get_team_mission_node(mission_id, from_node_id):
        return tool_error(f"from_node_id '{from_node_id}' does not exist.")
    if not db.get_team_mission_node(mission_id, to_node_id):
        return tool_error(f"to_node_id '{to_node_id}' does not exist.")
    metadata = args.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return tool_error("metadata must be an object.")
    edge = db.upsert_team_mission_edge(
        mission_id=mission_id,
        edge_id=_text(args.get("edge_id") or args.get("edgeId") or args.get("id")),
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        kind=_text(args.get("kind")) or "depends_on",
        metadata=_metadata_with_task_context(
            metadata,
            run_id=run_id,
            task_id=task_id,
            task_title=task_title,
            task_objective=task_objective,
        ),
    )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=run_id,
        event={"type": "mission.edge.created", "payload": {"edge": edge}},
    )
    reduced = db.reduce_team_mission_graph(mission_id)
    graph = reduced.get("graph") if isinstance(reduced, dict) and reduced.get("graph") else db.get_team_mission_graph(mission_id)
    return tool_result(
        success=True,
        mission_id=mission_id,
        edge=edge,
        graph_summary=_graph_summary(graph),
    )


def _handle_plan_complete(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    ctx = _authorized_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, run_id, binding, _mission, _graph, _strategy, planning_node = ctx
    mission_id = _text(binding.get("mission_id"))
    task_id = _task_id_from_context(binding, planning_node)
    result = db.complete_team_mission_plan(
        mission_id=mission_id,
        run_id=run_id,
        task_id=task_id,
        event_source="team_mission_plan_complete_tool",
    )
    if not result:
        return tool_error("Team Mission plan completion failed.")
    graph = result.get("graph") if isinstance(result.get("graph"), dict) else db.get_team_mission_graph(mission_id)
    return tool_result(
        success=True,
        mission_id=mission_id,
        mission_status=_text(result.get("mission_status")),
        approval_requests=list(result.get("approval_requests") or []),
        auto_start_ready_nodes=bool(result.get("auto_start_ready_nodes")),
        graph_summary=_graph_summary(graph),
        graph=graph,
    )


registry.register(
    name="team_mission_node_create",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_node_create",
        "description": (
            "Create a non-running node in the current Hermes Team Mission graph during "
            "Leader planning. Use this to decompose the mission before approval. "
            "Use canonical node kinds and valid Team Mission members only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Stable unique node id inside this mission graph."},
                "kind": {
                    "type": "string",
                    "description": "Canonical node kind: worker, verifier, or synthesis. Put specialties such as research, analysis, testing, or verification in metadata.work_type. Root and approval_gate are reserved.",
                },
                "title": {"type": "string", "description": "Short user-visible task title."},
                "objective": {"type": "string", "description": "Detailed objective for this node."},
                "status": {
                    "type": "string",
                    "description": "Initial non-running status. Prefer ready for executable planned work or todo when it depends on future work.",
                },
                "assignee_profile_id": {"type": "string", "description": "Optional Hermes profile id assigned to this node."},
                "assignee_profile_version_id": {"type": "string", "description": "Optional Hermes profile version id assigned to this node."},
                "assignee_member_id": {"type": "string", "description": "Team member id assigned to execute or own this node. Must match the Team Mission member list; never pass a run_id, session_id, or node_id. Omit for verifier/synthesis nodes unless using the Leader member id."},
                "assignee_role": {"type": "string", "description": "Optional role hint when assigning by team role instead of member id."},
                "runtime_scope_key": {"type": "string", "description": "Optional runtime scope key. Leave empty unless the plan requires a fixed scope."},
                "output_contract": {"type": "object", "description": "Expected output shape/contract for the node."},
                "metadata": {"type": "object", "description": "Additional structured planning metadata."},
                "position_x": {"type": "number", "description": "Optional graph x coordinate."},
                "position_y": {"type": "number", "description": "Optional graph y coordinate."},
            },
            "required": ["node_id", "title", "objective"],
        },
    },
    handler=_handle_node_create,
    emoji="",
)

registry.register(
    name="team_mission_edge_create",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_edge_create",
        "description": (
            "Create a dependency edge in the current Hermes Team Mission graph during "
            "Leader planning. Use this after creating the nodes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "from_node_id": {"type": "string", "description": "Source dependency node id."},
                "to_node_id": {"type": "string", "description": "Target node id that depends on the source."},
                "edge_id": {"type": "string", "description": "Optional stable edge id."},
                "kind": {"type": "string", "description": "Edge kind. Defaults to depends_on."},
                "metadata": {"type": "object", "description": "Additional structured dependency metadata."},
            },
            "required": ["from_node_id", "to_node_id"],
        },
    },
    handler=_handle_edge_create,
    emoji="",
)

registry.register(
    name="team_mission_plan_complete",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_plan_complete",
        "description": (
            "Mark the current Hermes Team Mission graph planning phase complete. "
            "In supervised mode this creates the approval gate and stops execution until approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    handler=_handle_plan_complete,
    emoji="",
)
