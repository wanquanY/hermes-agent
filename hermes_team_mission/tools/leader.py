"""Hermes Team Mission Leader tools.

These tools are exposed to Team Mission Leaders. Conversation turns can route a
user message into a new mission task. Bound Leader node runs can inspect mission
state through the read-only toolset while phase-specific mutation remains owned
by the separate ``team_mission_planning`` toolset.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from hermes_team_mission.domain.modes import MODE_AUTONOMOUS_MISSION
from hermes_team_mission.domain.modes import MODE_SUPERVISED_MISSION
from hermes_team_mission.runtime.profile_scope import gateway_call as _gateway_call
from hermes_team_mission.runtime.profile_scope import team_mission_control_db as _team_mission_control_db
from hermes_team_mission.runtime.profile_scope import unwrap_response as _unwrap_response
from tools.registry import registry, tool_error, tool_result
from hermes_team_mission.tools.profile import _handle_team_profile  # compatibility export
from hermes_team_mission.tools.profile import _leader_run_context


_READ_TOOLSET = "team_mission_read"
_CONVERSATION_TOOLSET = "team_mission_conversation_leader"
_TEAM_TASK_PLANNING_MODES = {MODE_SUPERVISED_MISSION, MODE_AUTONOMOUS_MISSION}
_START_TASK_RESULT_MESSAGE = (
    "Team mission task accepted. A new asynchronous team task was created "
    "and execution is now owned by the Team Mission runtime."
)
_DOVIE_ATTRIBUTION_CONTEXT_KEYS = (
    "cloud_query",
    "cloudQuery",
    "sourceAgentProfileId",
    "source_agent_profile_id",
    "sourceSessionId",
    "source_session_id",
    "sourceRunId",
    "source_run_id",
    "sourceTurnId",
    "source_turn_id",
    "sourceClientMessageId",
    "source_client_message_id",
    "root_agent_profile_id",
    "rootAgentProfileId",
    "executing_agent_profile_id",
    "executingAgentProfileId",
    "agent_role",
    "agentRole",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _get_db(parent_agent=None):
    return _team_mission_control_db(parent_agent)


def _session_context() -> dict[str, Any]:
    try:
        from channels.session_context import get_session_env

        raw = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
    except Exception:
        raw = ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _team_context() -> dict[str, Any] | str:
    context = _session_context()
    team = context.get("team_mission") if isinstance(context.get("team_mission"), Mapping) else {}
    if _text(team.get("kind")) != "leader_conversation":
        return "This tool can only be used from the Team Mission Leader conversation turn."
    if not (
        _text(team.get("conversation_id") or team.get("conversationId"))
        or _text(team.get("conversation_session_id") or team.get("conversationSessionId"))
        or _text(team.get("mission_id") or team.get("missionId"))
    ):
        return "Team Mission conversation context is not available for this Leader turn."
    return dict(team)


def _session_dovie_attribution_context() -> dict[str, Any]:
    context = _session_context()
    carried: dict[str, Any] = {}
    for key in _DOVIE_ATTRIBUTION_CONTEXT_KEYS:
        value = context.get(key)
        if value in (None, "", {}, []):
            continue
        carried[key] = dict(value) if isinstance(value, Mapping) else value
    return carried


def _active_mission_graph(db, team_context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    mission_id = _text(team_context.get("mission_id") or team_context.get("missionId"))
    if mission_id:
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
        if graph:
            return mission_id, graph
    identifier = _text(
        team_context.get("conversation_id")
        or team_context.get("conversationId")
        or team_context.get("conversation_session_id")
        or team_context.get("conversationSessionId")
    )
    resolved = db.resolve_team_mission_conversation(identifier) if identifier else {}
    graph = resolved.get("graph") if isinstance(resolved, dict) and isinstance(resolved.get("graph"), dict) else {}
    mission = resolved.get("mission") if isinstance(resolved, dict) and isinstance(resolved.get("mission"), dict) else {}
    return _text(mission.get("mission_id")), graph


def _graph_summary(graph: dict[str, Any]) -> dict[str, Any]:
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    edges = graph.get("edges") if isinstance(graph, dict) else []
    compact_nodes = []
    for node in nodes[:40] if isinstance(nodes, list) else []:
        if not isinstance(node, Mapping):
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        compact_nodes.append({
            "id": _text(node.get("node_id")),
            "kind": _text(node.get("kind")),
            "title": _text(node.get("title")),
            "status": _text(node.get("status")),
            "role": _text(metadata.get("role")),
            "phase": _text(metadata.get("phase")),
        })
    return {
        "mission": {
            "id": _text((mission or {}).get("mission_id")),
            "title": _text((mission or {}).get("title")),
            "objective": _text((mission or {}).get("objective")),
            "mode": _text((mission or {}).get("mode")),
            "status": _text((mission or {}).get("status")),
        },
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
        "edge_count": len(edges) if isinstance(edges, list) else 0,
        "nodes": compact_nodes,
    }


def _root_leader_node(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, Mapping):
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        if _text(node.get("kind")) == "root" and _text(metadata.get("role") or "leader") in {"leader", "lead", "root"}:
            return dict(node)
    for node in nodes if isinstance(nodes, list) else []:
        if isinstance(node, Mapping) and _text(node.get("kind")) == "root":
            return dict(node)
    return {}


def _active_run_id(parent_agent=None) -> str:
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


def _leader_member(team_context: dict[str, Any]) -> dict[str, Any]:
    members = team_context.get("members") if isinstance(team_context.get("members"), list) else []
    return next(
        (
            dict(item) for item in members
            if isinstance(item, Mapping) and _text(item.get("role")) in {"lead", "leader"}
        ),
        None,
    ) or next((dict(item) for item in members if isinstance(item, Mapping)), {})


def _task_execution_mode(team_context: Mapping[str, Any]) -> str:
    context_mode = _text(
        team_context.get("task_execution_mode")
        or team_context.get("taskExecutionMode")
        or team_context.get("mission_mode")
        or team_context.get("missionMode")
        or team_context.get("mode")
    )
    if context_mode in _TEAM_TASK_PLANNING_MODES:
        return context_mode
    return MODE_SUPERVISED_MISSION


def _handle_status(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    ctx = _team_context()
    db = _get_db(parent_agent)
    if not isinstance(ctx, str):
        team_context = ctx
        mission_id, graph = _active_mission_graph(db, team_context)
        memory = team_context.get("memory") if isinstance(team_context.get("memory"), Mapping) else {}
        return tool_result(
            success=True,
            mission_id=mission_id,
            graph_summary=_graph_summary(graph),
            memory=memory,
        )
    leader_run_ctx = _leader_run_context(args, parent_agent)
    if isinstance(leader_run_ctx, str):
        return tool_error(f"{ctx} Leader run context: {leader_run_ctx}")
    _db, _run_id, binding, _mission, _node = leader_run_ctx
    mission_id = _text(binding.get("mission_id"))
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    return tool_result(
        success=True,
        mission_id=mission_id,
        graph_summary=_graph_summary(graph),
        memory={},
    )


def _handle_start_task(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    ctx = _team_context()
    if isinstance(ctx, str):
        return tool_error(ctx)
    team_context = ctx
    db = _get_db(parent_agent)
    objective = _text(args.get("objective") or args.get("task") or args.get("prompt"))
    if not objective:
        return tool_error("objective is required.")
    title = _text(args.get("title")) or objective[:80] or "Team task"
    task_id = _text(args.get("task_id") or args.get("taskId")) or f"task-{uuid.uuid4().hex[:12]}"
    mission_id = _text(args.get("mission_id") or args.get("missionId")) or f"mission-{uuid.uuid4().hex[:16]}"
    conversation_id = _text(team_context.get("conversation_id") or team_context.get("conversationId"))
    conversation_session_id = _text(team_context.get("conversation_session_id") or team_context.get("conversationSessionId"))
    if not conversation_id:
        old_mission_id, old_graph = _active_mission_graph(db, team_context)
        old_mission = old_graph.get("mission") if isinstance(old_graph, dict) else {}
        conversation_id = _text((old_mission or {}).get("conversation_id")) or old_mission_id
    if not conversation_id and conversation_session_id:
        resolved = db.resolve_team_mission_conversation(conversation_session_id)
        conversation = resolved.get("conversation") if isinstance(resolved, dict) and isinstance(resolved.get("conversation"), Mapping) else {}
        conversation_id = _text(conversation.get("conversation_id"))
    if not conversation_session_id:
        resolved = db.resolve_team_mission_conversation(conversation_id) if conversation_id else {}
        conversation = resolved.get("conversation") if isinstance(resolved, dict) and isinstance(resolved.get("conversation"), Mapping) else {}
        conversation_session_id = _text(conversation.get("conversation_session_id")) or conversation_id
    if not conversation_id or not conversation_session_id:
        return tool_error("Team Mission conversation context is not available for this Leader turn.")
    active_run_id = _active_run_id(parent_agent)
    request_activity_id = _text(
        team_context.get("request_activity_id")
        or team_context.get("requestActivityId")
        or team_context.get("activity_id")
        or team_context.get("activityId")
    )
    existing_mission_id, existing_graph = _active_mission_graph(db, {
        **team_context,
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
    })
    existing_mission = existing_graph.get("mission") if isinstance(existing_graph, dict) and isinstance(existing_graph.get("mission"), Mapping) else {}
    existing_metadata = existing_mission.get("metadata") if isinstance(existing_mission.get("metadata"), Mapping) else {}
    existing_task = existing_metadata.get("active_task") if isinstance(existing_metadata.get("active_task"), Mapping) else {}
    existing_started_run_id = _text(existing_metadata.get("started_from_leader_conversation_run_id"))
    existing_task_id = _text(
        existing_metadata.get("active_task_id")
        or existing_metadata.get("task_id")
        or existing_task.get("task_id")
        or existing_task.get("taskId")
    )
    if existing_mission_id and (
        (active_run_id and existing_started_run_id == active_run_id)
        or (task_id and existing_task_id == task_id)
    ):
        node = _root_leader_node(existing_graph)
        mission_status = _text(existing_mission.get("status")) or "planning"
        return tool_result(
            success=True,
            intent="start_team_task",
            submission_status="accepted",
            task_status=mission_status,
            completion_status="pending",
            final_result_available=False,
            await_final_deliverable=True,
            mission_id=existing_mission_id,
            activity_id=request_activity_id,
            conversation_id=conversation_id,
            task_id=existing_task_id or task_id,
            node=node or {},
            run={},
            graph_summary=_graph_summary(existing_graph),
            message=_START_TASK_RESULT_MESSAGE,
            idempotent=True,
            hermes_control={
                "kind": "team_mission_started",
                "await_final_deliverable": True,
                "mission_status": mission_status,
            },
        )
    members = list(team_context.get("members") or []) if isinstance(team_context.get("members"), list) else []
    conversation_mode = _text(team_context.get("mode"))
    task_execution_mode = _task_execution_mode(team_context)
    metadata = {
        "conversation_id": conversation_id,
        "conversationTeamSessionId": conversation_session_id,
        "started_from_leader_conversation_run_id": active_run_id,
        "task_id": task_id,
        "task_title": title,
        "task_objective": objective,
        "conversation_mode": conversation_mode,
        "task_execution_mode": task_execution_mode,
        **({"request_activity_id": request_activity_id} if request_activity_id else {}),
        **({"dispatch_activity_id": request_activity_id} if request_activity_id else {}),
        **({"parent_activity_id": request_activity_id} if request_activity_id else {}),
    }
    product_context = _session_dovie_attribution_context()
    create_response = _gateway_call(
        "team_mission.create",
        {
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "team_id": _text(team_context.get("team_id") or team_context.get("teamId")),
            "title": title,
            "objective": objective,
            "mode": task_execution_mode,
            "workspace": {
                "workspace_id": _text(team_context.get("workspace_id") or team_context.get("workspaceId")),
                "workspace_path": _text(team_context.get("workspace_path") or team_context.get("workspacePath")),
            },
            "members": members,
            "task_id": task_id,
            **({"activity_id": request_activity_id} if request_activity_id else {}),
            "record_user_task_message": False,
            "metadata": metadata,
            **({"dovie_product_context": product_context} if product_context else {}),
        },
    )
    created, error = _unwrap_response(create_response)
    if error:
        return tool_error(error)
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    started = created.get("leader_start") if isinstance(created.get("leader_start"), Mapping) else {}
    node = started.get("node") if isinstance(started.get("node"), Mapping) else _root_leader_node(graph)
    mission = graph.get("mission") if isinstance(graph, dict) and isinstance(graph.get("mission"), Mapping) else {}
    mission_status = _text(mission.get("status")) or "planning"
    return tool_result(
        success=True,
        intent="start_team_task",
        submission_status="accepted",
        task_status=mission_status,
        completion_status="pending",
        final_result_available=False,
        await_final_deliverable=True,
        mission_id=mission_id,
        activity_id=request_activity_id,
        conversation_id=conversation_id,
        task_id=task_id,
        node=node or {},
        run=started.get("run") if isinstance(started, Mapping) else {},
        graph_summary=_graph_summary(graph),
        message=_START_TASK_RESULT_MESSAGE,
        hermes_control={
            "kind": "team_mission_started",
            "await_final_deliverable": True,
            "mission_status": mission_status,
        },
    )


registry.register(
    name="team_mission_status",
    toolset=_READ_TOOLSET,
    schema={
        "name": "team_mission_status",
        "description": "Read the current Hermes Team Mission graph and memory summary for a Team Mission Leader.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    handler=_handle_status,
    emoji="",
)

registry.register(
    name="team_mission_start_task",
    toolset=_CONVERSATION_TOOLSET,
    schema={
        "name": "team_mission_start_task",
        "description": (
            "Start a new Hermes Team Mission planning task from the current Leader conversation. "
            "Use only in the stable Leader conversation, and only when the user is clearly asking "
            "for a substantive executable team task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short visible task title."},
                "objective": {"type": "string", "description": "Self-contained objective for the new team task."},
                "task_id": {"type": "string", "description": "Optional stable task id."},
                "node_id": {"type": "string", "description": "Optional stable root planning node id."},
            },
            "required": ["objective"],
        },
    },
    handler=_handle_start_task,
    emoji="",
)


__all__ = [name for name in globals() if not name.startswith("__")]
