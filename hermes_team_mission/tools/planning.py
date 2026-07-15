"""Internal Hermes Team Mission planning tools.

These tools are intentionally available only through the internal
``team_mission_planning`` toolset.  They let a Team Mission Leader run mutate
the native Hermes mission graph during the planning/change-request phase while
keeping all authorization anchored to the run binding persisted by Hermes.
"""

from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from typing import Any

from hermes_team_mission.context.worker_context import NODE_BRIEF_BACKGROUND_MAX_CHARS
from hermes_team_mission.context.worker_context import NODE_BRIEF_GOAL_MAX_CHARS
from hermes_team_mission.context.worker_context import NODE_BRIEF_ITEM_MAX_CHARS
from hermes_team_mission.context.worker_context import NODE_BRIEF_LIST_MAX_ITEMS
from hermes_team_mission.context.worker_context import NODE_OBJECTIVE_MAX_CHARS
from hermes_team_mission.context.worker_context import NODE_TITLE_MAX_CHARS
from hermes_team_mission.context.worker_context import TOOL_ARGS_BUDGET_CHARS
from hermes_team_mission.context.worker_context import TOOL_RESULT_BUDGET_CHARS
from hermes_team_mission.context.worker_context import bounded_task_brief
from hermes_team_mission.context.worker_context import cap_text
from hermes_team_mission.context.worker_context import task_brief_budget_violations
from hermes_team_mission.context.worker_context import team_mission_graph_slice
from hermes_team_mission.context.worker_context import team_mission_graph_summary
from hermes_team_mission.domain.assignees import normalized_member_dicts
from hermes_team_mission.domain.modes import strategy_for_mode
from hermes_team_mission.domain.node_kinds import metadata_with_normalized_node_kind
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind
from hermes_team_mission.runtime.profile_scope import team_mission_control_db as _team_mission_control_db
from tools.registry import registry, tool_error, tool_result


_TOOLSET = "team_mission_planning"
_LEADER_ROLES = {"leader", "root"}
_LEADER_NODE_KINDS = {"root"}
_MUTATION_PHASES = {"planning", "change_request"}
_RESERVED_NODE_KINDS = {"root", "approval_gate"}
_EXECUTABLE_NODE_KINDS = {"worker", "verifier", "synthesis"}
_PLANNED_FINALIZER_NODE_KINDS = {"verifier", "synthesis"}
_RUNNING_STATUSES = {"running", "starting", "completed", "verified", "failed", "cancelled", "interrupted"}
_UNKNOWN_MARKERS = {
    "unknown",
    "unclear",
    "not sure",
    "n/a",
    "na",
    "none",
    "tbd",
    "todo",
    "待确认",
    "不确定",
    "不清楚",
    "未知",
    "无",
}
_GRAPH_SLICE_INCLUDE_BRIEF_VALUES = {"none", "summary", "capped"}
_TASK_BRIEF_LIST_FIELDS = {"execution", "acceptance_criteria", "constraints", "inputs", "deliverables"}
_TASK_BRIEF_TEXT_FIELDS = {"background", "goal"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\r", "\n").split("\n")
    elif isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = (value,)
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        item_text = _text(item)
        if item_text and item_text not in seen:
            seen.add(item_text)
            result.append(item_text)
    return result


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return len(str(value or ""))


def _payload_budget_error(args: Mapping[str, Any]) -> str:
    size = _json_size(args)
    if size <= TOOL_ARGS_BUDGET_CHARS:
        return ""
    return (
        f"Team Mission planning tool arguments are too large ({size} chars). "
        f"Keep each tool call under {TOOL_ARGS_BUDGET_CHARS} chars. "
        "Create one node at a time and move long node detail into smaller, concrete task_brief fields."
    )


def _length_error(field: str, value: str, limit: int) -> str:
    if len(value) <= limit:
        return ""
    return f"{field} is too long ({len(value)} chars). Keep it under {limit} chars and move detail into bounded task_brief fields."


def _list_length_error(field: str, values: list[str], *, action: str = "Split into multiple node_create calls") -> str:
    count = len(values)
    if count <= NODE_BRIEF_LIST_MAX_ITEMS:
        return ""
    return (
        f"task_brief.{field} has too many items: max {NODE_BRIEF_LIST_MAX_ITEMS}; got {count}. "
        f"{action} or move detail to artifact files."
    )


def _raw_idempotency_key(args: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return _text(
        args.get("idempotency_key")
        or args.get("idempotencyKey")
        or metadata.get("idempotency_key")
        or metadata.get("idempotencyKey")
    )


def _idempotency_key(
    args: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
    *,
    mission_id: str,
    run_id: str,
    tool_name: str,
) -> str:
    raw_key = _raw_idempotency_key(args, metadata)
    if not raw_key:
        return ""
    return f"{mission_id}:{run_id}:{tool_name}:{raw_key}"


def _idempotency_fingerprint(args: Mapping[str, Any]) -> str:
    ignored = {"idempotency_key", "idempotencyKey", "run_id", "runId"}
    semantic_args = {key: value for key, value in args.items() if key not in ignored}
    try:
        rendered = json.dumps(semantic_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        rendered = str(sorted((str(key), str(value)) for key, value in semantic_args.items()))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _idempotency_conflict(existing: Mapping[str, Any], fingerprint: str) -> str:
    if not existing or not fingerprint:
        return ""
    metadata = _metadata(existing.get("metadata"))
    existing_fingerprint = _text(metadata.get("idempotency_fingerprint") or metadata.get("idempotencyFingerprint"))
    if existing_fingerprint and existing_fingerprint != fingerprint:
        return (
            "idempotency_key was already used with different arguments in this planner run. "
            "Reuse the same arguments for retries, or choose a new idempotency_key for a distinct node/edge."
        )
    return ""


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(mapping.get(key))
        if value:
            return value
    return ""


def _unknownish(value: Any) -> bool:
    values = _text_list(value)
    if not values:
        return True
    for item in values:
        normalized = item.strip().lower().strip("。.!?？")
        if normalized in _UNKNOWN_MARKERS:
            return True
    return False


def _task_brief_from_args(
    args: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    objective: str,
    kind: str,
) -> tuple[dict[str, Any], str]:
    if kind not in _EXECUTABLE_NODE_KINDS:
        return {}, ""
    raw = (
        args.get("task_brief")
        or args.get("taskBrief")
        or metadata.get("task_brief")
        or metadata.get("taskBrief")
        or {}
    )
    if raw and not isinstance(raw, Mapping):
        return {}, "task_brief must be an object."
    raw = raw if isinstance(raw, Mapping) else {}
    candidate = {
        "background": _first_text(
            raw,
            "background",
            "context",
            "business_context",
            "businessContext",
        ) or _first_text(args, "background", "context"),
        "execution": _text_list(
            raw.get("execution")
            or raw.get("execution_steps")
            or raw.get("executionSteps")
            or raw.get("work_items")
            or raw.get("workItems")
            or args.get("execution")
            or args.get("execution_steps")
            or args.get("executionSteps")
        ),
        "goal": _first_text(
            raw,
            "goal",
            "target",
            "deliverable_goal",
            "deliverableGoal",
        ) or _first_text(args, "goal", "target", "deliverable_goal", "deliverableGoal") or objective,
        "acceptance_criteria": _text_list(
            raw.get("acceptance_criteria")
            or raw.get("acceptanceCriteria")
            or raw.get("verification")
            or raw.get("done_when")
            or raw.get("doneWhen")
            or args.get("acceptance_criteria")
            or args.get("acceptanceCriteria")
        ),
        "constraints": _text_list(
            raw.get("constraints")
            or raw.get("requirements")
            or raw.get("must_not")
            or raw.get("mustNot")
            or args.get("constraints")
        ),
        "inputs": _text_list(raw.get("inputs") or raw.get("references") or args.get("inputs")),
        "deliverables": _text_list(raw.get("deliverables") or raw.get("outputs") or args.get("deliverables")),
    }
    for field in _TASK_BRIEF_LIST_FIELDS:
        error = _list_length_error(field, candidate.get(field) or [])
        if error:
            return {}, error
    violations = task_brief_budget_violations(candidate)
    if violations:
        return {}, " ".join(violations) + " Split long content across smaller node_create calls or artifact files."
    brief = bounded_task_brief(candidate)
    missing = []
    for key in ("background", "execution", "goal", "acceptance_criteria"):
        if _unknownish(brief.get(key)):
            missing.append(key)
    if missing:
        return {}, (
            "task_brief is required for executable Team Mission nodes. "
            "Include concrete task_brief.background, task_brief.execution, "
            "task_brief.goal, and task_brief.acceptance_criteria. "
            f"Missing or unclear fields: {', '.join(missing)}. "
            "If any required field is unknown, call clarify before creating the node."
        )
    return {key: value for key, value in brief.items() if value not in ("", [])}, ""


def _output_contract_with_brief(output_contract: Mapping[str, Any], brief: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(output_contract)
    if not brief:
        return result
    result.setdefault("format", "structured_deliverable")
    result.setdefault("delivery_channel", "handoff")
    result.setdefault("requires_explicit_handoff", True)
    if brief.get("goal"):
        result.setdefault("goal", brief["goal"])
    if brief.get("deliverables"):
        result.setdefault("deliverables", brief["deliverables"])
    if brief.get("acceptance_criteria"):
        result.setdefault("acceptance_criteria", brief["acceptance_criteria"])
    result.setdefault("requires_clarification_when_blocked", True)
    return result


def _canonical_task_brief_field(value: Any) -> str:
    field = _text(value)
    aliases = {
        "acceptanceCriteria": "acceptance_criteria",
        "acceptance": "acceptance_criteria",
        "executionSteps": "execution",
        "execution_steps": "execution",
        "outputs": "deliverables",
        "references": "inputs",
    }
    return aliases.get(field, field)


def _append_task_brief_value(brief: Mapping[str, Any], field: str, value: Any) -> tuple[dict[str, Any], str]:
    field = _canonical_task_brief_field(field)
    if field not in _TASK_BRIEF_TEXT_FIELDS and field not in _TASK_BRIEF_LIST_FIELDS:
        return {}, (
            "field must be one of background, goal, execution, acceptance_criteria, "
            "constraints, inputs, or deliverables."
        )
    updated = dict(brief)
    if field in _TASK_BRIEF_TEXT_FIELDS:
        incoming = _text(value)
        if not incoming:
            return {}, "text is required for task_brief text fields."
        separator = "\n" if _text(updated.get(field)) else ""
        updated[field] = f"{_text(updated.get(field))}{separator}{incoming}"
    else:
        incoming_items = _text_list(value)
        if not incoming_items:
            return {}, "items are required for task_brief list fields."
        existing = _text_list(updated.get(field))
        updated[field] = existing + incoming_items
        error = _list_length_error(field, updated[field], action="Append fewer items in this call")
        if error:
            return {}, error
    violations = task_brief_budget_violations(updated)
    if violations:
        return {}, " ".join(violations)
    return bounded_task_brief(updated), ""


def _number(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_db(parent_agent=None):
    return _team_mission_control_db(parent_agent)


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
    return team_mission_graph_summary(graph)


def _node_ack_from_graph(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    graph_slice = team_mission_graph_slice(
        graph,
        node_ids=[node_id],
        include_dependencies=False,
        include_brief="none",
        limit=1,
    )
    nodes = graph_slice.get("nodes") if isinstance(graph_slice.get("nodes"), list) else []
    return nodes[0] if nodes else {"node_id": node_id}


def _approval_summary(approval_requests: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in approval_requests if isinstance(approval_requests, list) else []:
        if not isinstance(item, Mapping):
            continue
        result.append({
            "approval_id": _text(item.get("approval_id") or item.get("approvalId") or item.get("id")),
            "scope": _text(item.get("scope")),
            "status": _text(item.get("status")),
            "node_id": _text(item.get("node_id") or item.get("nodeId")),
            "task_id": _text(item.get("task_id") or item.get("taskId")),
        })
    return [{key: value for key, value in item.items() if value} for item in result]


def _existing_node_by_idempotency(db: Any, mission_id: str, idempotency_key: str) -> dict[str, Any]:
    if not idempotency_key:
        return {}
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    for node in graph.get("nodes") if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) else []:
        if not isinstance(node, Mapping):
            continue
        node_metadata = _metadata(node.get("metadata"))
        if _text(node_metadata.get("idempotency_key") or node_metadata.get("idempotencyKey")) == idempotency_key:
            return dict(node)
    return {}


def _existing_edge_by_idempotency(db: Any, mission_id: str, idempotency_key: str) -> dict[str, Any]:
    if not idempotency_key:
        return {}
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    for edge in graph.get("edges") if isinstance(graph, dict) and isinstance(graph.get("edges"), list) else []:
        if not isinstance(edge, Mapping):
            continue
        edge_metadata = _metadata(edge.get("metadata"))
        if _text(edge_metadata.get("idempotency_key") or edge_metadata.get("idempotencyKey")) == idempotency_key:
            return dict(edge)
    return {}


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


def _node_task_id(node: Mapping[str, Any]) -> str:
    metadata = _metadata(node.get("metadata"))
    task = metadata.get("active_task") if isinstance(metadata.get("active_task"), Mapping) else {}
    return _text(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or task.get("task_id")
        or task.get("taskId")
    )


def _node_matches_task(node: Mapping[str, Any], task_id: str) -> bool:
    if not task_id:
        return True
    node_task_id = _node_task_id(node)
    if node_task_id:
        return node_task_id == task_id
    return task_id in _text(node.get("node_id"))


def _plan_finalizer_validation_error(graph: Mapping[str, Any], task_id: str) -> str:
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else []
    scoped_nodes = [
        node
        for node in nodes
        if isinstance(node, Mapping)
        and _node_matches_task(node, task_id)
        and normalize_team_mission_node_kind(node.get("kind")) not in {"root", "approval_gate"}
    ]
    work_nodes = [
        node
        for node in scoped_nodes
        if normalize_team_mission_node_kind(node.get("kind")) not in _PLANNED_FINALIZER_NODE_KINDS
    ]
    finalizer_kinds = {
        normalize_team_mission_node_kind(node.get("kind"))
        for node in scoped_nodes
        if normalize_team_mission_node_kind(node.get("kind")) in _PLANNED_FINALIZER_NODE_KINDS
    }
    missing = [kind for kind in ("verifier", "synthesis") if kind not in finalizer_kinds]
    if not work_nodes:
        return (
            "team_mission_plan_complete requires at least one worker node. "
            "Create concrete worker work before completing the plan."
        )
    if missing:
        return (
            "team_mission_plan_complete requires the Leader-planned graph to include "
            "verifier and synthesis nodes. Missing: "
            f"{', '.join(missing)}. Create these nodes explicitly and connect dependencies before completing the plan."
        )
    return ""


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
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
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
    args = args if isinstance(args, dict) else {}
    budget_error = _payload_budget_error(args)
    if budget_error:
        return tool_error(budget_error)
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
    for field, value, limit in (
        ("title", title, NODE_TITLE_MAX_CHARS),
        ("objective", objective, NODE_OBJECTIVE_MAX_CHARS),
    ):
        field_error = _length_error(field, value, limit)
        if field_error:
            return tool_error(field_error)
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
    raw_idempotency_key = _raw_idempotency_key(args, metadata)
    idempotency_key = _idempotency_key(
        args,
        metadata,
        mission_id=mission_id,
        run_id=run_id,
        tool_name="team_mission_node_create",
    )
    idempotency_fingerprint = _idempotency_fingerprint(args) if idempotency_key else ""
    existing_idempotent_node = _existing_node_by_idempotency(db, mission_id, idempotency_key)
    if existing_idempotent_node:
        conflict = _idempotency_conflict(existing_idempotent_node, idempotency_fingerprint)
        if conflict:
            return tool_error(conflict)
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
        return tool_result(
            dovie_event="team_mission_node_created",
            success=True,
            mission_id=mission_id,
            node_id=_text(existing_idempotent_node.get("node_id")),
            node_summary=_node_ack_from_graph(graph, _text(existing_idempotent_node.get("node_id"))),
            graph_summary=_graph_summary(graph),
            idempotent=True,
            idempotency_key=idempotency_key,
        )
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
        metadata["idempotency_user_key"] = raw_idempotency_key
        metadata["idempotency_fingerprint"] = idempotency_fingerprint
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
    task_brief, task_brief_error = _task_brief_from_args(
        args,
        metadata,
        objective=objective,
        kind=kind,
    )
    if task_brief_error:
        return tool_error(task_brief_error)
    if task_brief:
        metadata["task_brief"] = task_brief
        metadata.setdefault("clarification_policy", {
            "when": "critical_input_missing_or_acceptance_unclear",
            "tool": "clarify",
            "instruction": "Ask the user before executing on guessed assumptions.",
        })
        output_contract = _output_contract_with_brief(output_contract, task_brief)
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
    graph = reduced.get("graph") if isinstance(reduced, dict) and reduced.get("graph") else db.team_mission_graphs.get_team_mission_graph(mission_id)
    return tool_result(
        dovie_event="team_mission_node_created",
        success=True,
        mission_id=mission_id,
        node_id=_text(node.get("node_id")),
        node_summary=_node_ack_from_graph(graph, _text(node.get("node_id"))),
        graph_summary=_graph_summary(graph),
    )


def _handle_edge_create(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    budget_error = _payload_budget_error(args)
    if budget_error:
        return tool_error(budget_error)
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
    metadata = dict(metadata)
    raw_idempotency_key = _raw_idempotency_key(args, metadata)
    idempotency_key = _idempotency_key(
        args,
        metadata,
        mission_id=mission_id,
        run_id=run_id,
        tool_name="team_mission_edge_create",
    )
    idempotency_fingerprint = _idempotency_fingerprint(args) if idempotency_key else ""
    existing_idempotent_edge = _existing_edge_by_idempotency(db, mission_id, idempotency_key)
    if existing_idempotent_edge:
        conflict = _idempotency_conflict(existing_idempotent_edge, idempotency_fingerprint)
        if conflict:
            return tool_error(conflict)
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
        return tool_result(
            dovie_event="team_mission_edge_created",
            success=True,
            mission_id=mission_id,
            edge_id=_text(existing_idempotent_edge.get("edge_id")),
            edge_summary={
                "edge_id": _text(existing_idempotent_edge.get("edge_id")),
                "from_node_id": _text(existing_idempotent_edge.get("from_node_id")),
                "to_node_id": _text(existing_idempotent_edge.get("to_node_id")),
                "kind": _text(existing_idempotent_edge.get("kind")),
            },
            graph_summary=_graph_summary(graph),
            idempotent=True,
            idempotency_key=idempotency_key,
        )
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
        metadata["idempotency_user_key"] = raw_idempotency_key
        metadata["idempotency_fingerprint"] = idempotency_fingerprint
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
    graph = reduced.get("graph") if isinstance(reduced, dict) and reduced.get("graph") else db.team_mission_graphs.get_team_mission_graph(mission_id)
    return tool_result(
        dovie_event="team_mission_edge_created",
        success=True,
        mission_id=mission_id,
        edge_id=_text(edge.get("edge_id")),
        edge_summary={
            "edge_id": _text(edge.get("edge_id")),
            "from_node_id": _text(edge.get("from_node_id")),
            "to_node_id": _text(edge.get("to_node_id")),
            "kind": _text(edge.get("kind")),
        },
        graph_summary=_graph_summary(graph),
    )


def _handle_node_brief_append(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    budget_error = _payload_budget_error(args)
    if budget_error:
        return tool_error(budget_error)
    ctx = _authorized_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, run_id, binding, mission, _graph, _strategy, planning_node = ctx
    mission_id = _text(binding.get("mission_id"))
    node_id = _text(args.get("node_id") or args.get("nodeId"))
    if not node_id:
        return tool_error("node_id is required.")
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return tool_error(f"node_id '{node_id}' does not exist.")
    field = _canonical_task_brief_field(args.get("field"))
    value = args.get("items") if field in _TASK_BRIEF_LIST_FIELDS else args.get("text")
    if value is None:
        value = args.get("value")
    metadata = _metadata(node.get("metadata"))
    existing_brief = metadata.get("task_brief") if isinstance(metadata.get("task_brief"), Mapping) else {}
    updated_brief, error = _append_task_brief_value(existing_brief, field, value)
    if error:
        return tool_error(error)
    metadata["task_brief"] = updated_brief
    task_id = _task_id_from_context(binding, planning_node)
    task_title = _task_text_from_context(binding, planning_node, "task_title", "taskTitle") or _text(planning_node.get("title"))
    task_objective = _task_text_from_context(binding, planning_node, "task_objective", "taskObjective") or _text(planning_node.get("objective"))
    output_contract = _output_contract_with_brief(_metadata(node.get("output_contract")), updated_brief)
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=_text(node.get("kind") or "worker"),
        title=_text(node.get("title")),
        objective=_text(node.get("objective")),
        status=_text(node.get("status") or "ready"),
        assignee_profile_id=_text(node.get("assignee_profile_id")),
        assignee_profile_version_id=_text(node.get("assignee_profile_version_id")),
        runtime_scope_key=_text(node.get("runtime_scope_key")),
        output_contract=output_contract,
        metadata=_metadata_with_task_context(
            metadata,
            run_id=run_id,
            task_id=task_id,
            task_title=task_title,
            task_objective=task_objective,
        ),
        position_x=_number(node.get("position_x")),
        position_y=_number(node.get("position_y")),
    )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=run_id,
        event={
            "type": "mission.node.updated",
            "payload": {
                "node_id": node_id,
                "field": "task_brief",
                "task_brief_field": field,
            },
        },
    )
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    return tool_result(
        dovie_event="team_mission_node_updated",
        success=True,
        mission_id=mission_id,
        node_id=node_id,
        task_brief_field=field,
        node_summary=_node_ack_from_graph(graph, node_id),
        graph_summary=_graph_summary(graph),
        task_brief_keys=sorted(updated_brief.keys()),
    )


def _handle_plan_complete(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    ctx = _authorized_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, run_id, binding, _mission, graph, _strategy, planning_node = ctx
    mission_id = _text(binding.get("mission_id"))
    task_id = _task_id_from_context(binding, planning_node)
    finalizer_error = _plan_finalizer_validation_error(graph, task_id)
    if finalizer_error:
        return tool_error(finalizer_error)
    result = db.complete_team_mission_plan(
        mission_id=mission_id,
        run_id=run_id,
        task_id=task_id,
        event_source="team_mission_plan_complete_tool",
    )
    if not result:
        return tool_error("Team Mission plan completion failed.")
    graph = result.get("graph") if isinstance(result.get("graph"), dict) else db.team_mission_graphs.get_team_mission_graph(mission_id)
    return tool_result(
        dovie_event="team_mission_plan_completed",
        success=True,
        mission_id=mission_id,
        mission_status=_text(result.get("mission_status")),
        approval_requests=_approval_summary(result.get("approval_requests")),
        auto_start_ready_nodes=bool(result.get("auto_start_ready_nodes")),
        graph_summary=_graph_summary(graph),
    )


def _handle_graph_summary(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    ctx = _authorized_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    _db, _run_id, binding, _mission, graph, _strategy, _planning_node = ctx
    try:
        limit = int(args.get("limit") or 24)
    except Exception:
        limit = 24
    return tool_result(
        success=True,
        mission_id=_text(binding.get("mission_id")),
        graph_summary=team_mission_graph_summary(graph, node_limit=max(1, min(limit, 50))),
    )


def _handle_graph_slice(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    ctx = _authorized_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    _db, _run_id, binding, _mission, graph, _strategy, _planning_node = ctx
    raw_node_ids = args.get("node_ids") or args.get("nodeIds") or args.get("node_id") or args.get("nodeId") or []
    if isinstance(raw_node_ids, str):
        node_ids = [item.strip() for item in raw_node_ids.replace(",", "\n").split("\n") if item.strip()]
    elif isinstance(raw_node_ids, (list, tuple, set)):
        node_ids = [_text(item) for item in raw_node_ids if _text(item)]
    else:
        node_ids = []
    include_brief = _text(args.get("include_brief") or args.get("includeBrief") or "summary").lower()
    if include_brief not in _GRAPH_SLICE_INCLUDE_BRIEF_VALUES:
        include_brief = "summary"
    try:
        limit = int(args.get("limit") or 30)
    except Exception:
        limit = 30
    return tool_result(
        success=True,
        mission_id=_text(binding.get("mission_id")),
        graph_slice=team_mission_graph_slice(
            graph,
            node_ids=node_ids,
            include_dependencies=bool(args.get("include_dependencies") if "include_dependencies" in args else args.get("includeDependencies", True)),
            include_brief=include_brief,
            limit=limit,
        ),
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
                "node_id": {"type": "string", "maxLength": 160, "description": "Stable unique node id inside this mission graph."},
                "kind": {
                    "type": "string",
                    "description": "Canonical node kind: worker, verifier, or synthesis. Put specialties such as research, analysis, testing, or verification in metadata.work_type. Root and approval_gate are reserved.",
                },
                "title": {"type": "string", "maxLength": NODE_TITLE_MAX_CHARS, "description": "Short user-visible task title."},
                "objective": {"type": "string", "maxLength": NODE_OBJECTIVE_MAX_CHARS, "description": "Concise node summary. For executable nodes, this is not enough by itself; also provide task_brief."},
                "status": {
                    "type": "string",
                    "description": "Initial non-running status. Prefer ready for executable planned work or todo when it depends on future work.",
                },
                "assignee_profile_id": {"type": "string", "maxLength": 160, "description": "Optional Hermes profile id assigned to this node."},
                "assignee_profile_version_id": {"type": "string", "maxLength": 160, "description": "Optional Hermes profile version id assigned to this node."},
                "assignee_member_id": {"type": "string", "maxLength": 160, "description": "Team member id assigned to execute or own this node. Must match the Team Mission member list; never pass a run_id, session_id, or node_id. Omit for verifier/synthesis nodes unless using the Leader member id."},
                "assignee_role": {"type": "string", "maxLength": 160, "description": "Optional role hint when assigning by team role instead of member id."},
                "runtime_scope_key": {"type": "string", "maxLength": 240, "description": "Optional runtime scope key. Leave empty unless the plan requires a fixed scope."},
                "task_brief": {
                    "type": "object",
                    "description": "Required for worker/verifier/synthesis nodes. Must include concrete background, execution, goal, and acceptance_criteria. If any of these are unclear, call clarify before creating the node.",
                    "properties": {
                        "background": {"type": "string", "maxLength": NODE_BRIEF_BACKGROUND_MAX_CHARS, "description": "Why this node exists: user context, source material, dependencies, constraints, and relevant prior decisions."},
                        "execution": {"type": "array", "maxItems": NODE_BRIEF_LIST_MAX_ITEMS, "items": {"type": "string", "maxLength": NODE_BRIEF_ITEM_MAX_CHARS}, "description": "Specific work the assignee must perform."},
                        "goal": {"type": "string", "maxLength": NODE_BRIEF_GOAL_MAX_CHARS, "description": "Desired outcome for this node."},
                        "acceptance_criteria": {"type": "array", "maxItems": NODE_BRIEF_LIST_MAX_ITEMS, "items": {"type": "string", "maxLength": NODE_BRIEF_ITEM_MAX_CHARS}, "description": "Observable checks that define done."},
                        "constraints": {"type": "array", "maxItems": NODE_BRIEF_LIST_MAX_ITEMS, "items": {"type": "string", "maxLength": NODE_BRIEF_ITEM_MAX_CHARS}, "description": "Limits, non-goals, risk boundaries, or style/quality requirements."},
                        "inputs": {"type": "array", "maxItems": NODE_BRIEF_LIST_MAX_ITEMS, "items": {"type": "string", "maxLength": NODE_BRIEF_ITEM_MAX_CHARS}, "description": "Files, URLs, upstream nodes, user-provided data, or artifacts to use."},
                        "deliverables": {"type": "array", "maxItems": NODE_BRIEF_LIST_MAX_ITEMS, "items": {"type": "string", "maxLength": NODE_BRIEF_ITEM_MAX_CHARS}, "description": "Concrete artifacts or response sections the node must produce."},
                    },
                },
                "output_contract": {"type": "object", "description": "Expected output shape/contract for the node. task_brief acceptance criteria are copied here when present."},
                "metadata": {"type": "object", "description": "Additional structured planning metadata. Do not hide required task_brief fields only in prose."},
                "idempotency_key": {"type": "string", "maxLength": 200, "description": "Stable key for retrying this exact graph mutation without creating duplicates."},
                "position_x": {"type": "number", "description": "Optional graph x coordinate."},
                "position_y": {"type": "number", "description": "Optional graph y coordinate."},
            },
            "required": ["node_id", "title", "objective"],
        },
    },
    handler=_handle_node_create,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
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
                "from_node_id": {"type": "string", "maxLength": 160, "description": "Source dependency node id."},
                "to_node_id": {"type": "string", "maxLength": 160, "description": "Target node id that depends on the source."},
                "edge_id": {"type": "string", "maxLength": 200, "description": "Optional stable edge id."},
                "kind": {"type": "string", "description": "Edge kind. Defaults to depends_on."},
                "metadata": {"type": "object", "description": "Additional structured dependency metadata."},
                "idempotency_key": {"type": "string", "maxLength": 200, "description": "Stable key for retrying this exact graph mutation without creating duplicates."},
            },
            "required": ["from_node_id", "to_node_id"],
        },
    },
    handler=_handle_edge_create,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)

registry.register(
    name="team_mission_node_brief_append",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_node_brief_append",
        "description": (
            "Append one bounded chunk to an existing Team Mission node task_brief during planning. "
            "Use this when a node brief must be built incrementally instead of sending one large node_create payload."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "maxLength": 160, "description": "Existing node id to update."},
                "field": {
                    "type": "string",
                    "enum": ["background", "goal", "execution", "acceptance_criteria", "constraints", "inputs", "deliverables"],
                    "description": "task_brief field to append.",
                },
                "text": {
                    "type": "string",
                    "maxLength": NODE_BRIEF_ITEM_MAX_CHARS,
                    "description": "Text chunk for background or goal.",
                },
                "items": {
                    "type": "array",
                    "maxItems": NODE_BRIEF_LIST_MAX_ITEMS,
                    "items": {"type": "string", "maxLength": NODE_BRIEF_ITEM_MAX_CHARS},
                    "description": "List items to append for execution, acceptance_criteria, constraints, inputs, or deliverables.",
                },
                "value": {
                    "description": "Fallback value when the caller cannot choose text/items; must still fit the target field budget.",
                },
            },
            "required": ["node_id", "field"],
        },
    },
    handler=_handle_node_brief_append,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)

registry.register(
    name="team_mission_plan_complete",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_plan_complete",
        "description": (
            "Mark the current Hermes Team Mission graph planning phase complete. "
            "The graph must already include at least one worker node plus explicit verifier and synthesis nodes. "
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
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)

registry.register(
    name="team_mission_graph_summary",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_graph_summary",
        "description": "Read a bounded summary of the current Team Mission graph. Use this after retries or before continuing planning; it never returns the full graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum compact nodes to include, capped at 50."},
            },
            "required": [],
        },
    },
    handler=_handle_graph_summary,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)

registry.register(
    name="team_mission_graph_slice",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_graph_slice",
        "description": "Read bounded details for selected Team Mission nodes and their dependencies. Use this instead of asking for the full graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 160},
                    "maxItems": 30,
                    "description": "Node ids to inspect. Leave empty only when you need the first page of graph nodes.",
                },
                "include_dependencies": {"type": "boolean", "description": "Include direct dependency neighbors. Defaults to true."},
                "include_brief": {"type": "string", "enum": ["none", "summary", "capped"], "description": "How much task_brief to include. Defaults to summary."},
                "limit": {"type": "integer", "description": "Maximum nodes to return, capped at 30."},
            },
            "required": [],
        },
    },
    handler=_handle_graph_slice,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)


__all__ = [name for name in globals() if not name.startswith("__")]
