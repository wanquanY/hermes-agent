"""Hermes Team Mission Leader conversation tools.

These tools are only exposed to the stable Team Mission conversation Leader
turn.  They let the Leader inspect mission state and explicitly promote a user
message into a new planning node. Graph decomposition remains owned by the
separate ``team_mission_planning`` toolset on the planning node run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from hermes_state import SessionDB
from tools.registry import registry, tool_error, tool_result


_TOOLSET = "team_mission_leader"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _get_db(parent_agent=None):
    db = getattr(parent_agent, "_session_db", None) if parent_agent is not None else None
    if db is not None:
        return db
    try:
        from tui_gateway import server

        return server._get_db()
    except Exception:
        return SessionDB()


def _session_context() -> dict[str, Any]:
    try:
        from gateway.session_context import get_session_env

        raw = get_session_env("HERMES_DOXIE_PRODUCT_CONTEXT", "")
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


def _active_mission_graph(db, team_context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    mission_id = _text(team_context.get("mission_id") or team_context.get("missionId"))
    if mission_id:
        graph = db.get_team_mission_graph(mission_id)
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


def _team_capability_payload(team_context: dict[str, Any]) -> dict[str, Any]:
    payload = team_context.get("team_capability") if isinstance(team_context.get("team_capability"), Mapping) else {}
    source_packet = team_context.get("team_capability_source_packet") if isinstance(team_context.get("team_capability_source_packet"), Mapping) else {}
    snapshot_id = _text(team_context.get("team_capability_snapshot_id") or team_context.get("teamCapabilitySnapshotId"))
    result = dict(payload)
    if source_packet:
        result.setdefault("source_packet", dict(source_packet))
    if snapshot_id:
        result.setdefault("snapshot_id", snapshot_id)
    return result


def _gateway_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        from tui_gateway import server

        fn = server._methods.get(method)
        if not callable(fn):
            return {"error": {"message": f"Gateway method {method} is unavailable."}}
        return fn(None, params)
    except Exception as exc:
        return {"error": {"message": str(exc)}}


def _unwrap_response(response: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(response, dict):
        return {}, "Gateway returned an invalid response."
    error = response.get("error")
    if isinstance(error, Mapping):
        return {}, _text(error.get("message")) or "Gateway method failed."
    result = response.get("result")
    return (dict(result), "") if isinstance(result, Mapping) else ({}, "")


def _handle_status(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    del args
    ctx = _team_context()
    if isinstance(ctx, str):
        return tool_error(ctx)
    team_context = ctx
    db = _get_db(parent_agent)
    mission_id, graph = _active_mission_graph(db, team_context)
    memory = team_context.get("memory") if isinstance(team_context.get("memory"), Mapping) else {}
    return tool_result(
        success=True,
        mission_id=mission_id,
        graph_summary=_graph_summary(graph),
        memory=memory,
    )


def _handle_team_profile(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    del args
    ctx = _team_context()
    if isinstance(ctx, str):
        return tool_error(ctx)
    team_context = ctx
    db = _get_db(parent_agent)
    mission_id, _graph = _active_mission_graph(db, team_context)
    params: dict[str, Any] = {}
    if mission_id:
        params["mission_id"] = mission_id
    else:
        conversation_id = _text(team_context.get("conversation_id") or team_context.get("conversationId"))
        conversation_session_id = _text(team_context.get("conversation_session_id") or team_context.get("conversationSessionId"))
        if conversation_id:
            params["conversation_id"] = conversation_id
        if conversation_session_id:
            params["conversation_session_id"] = conversation_session_id
    snapshot_id = _text(team_context.get("team_capability_snapshot_id") or team_context.get("teamCapabilitySnapshotId"))
    if snapshot_id:
        params["snapshot_id"] = snapshot_id
    response = _gateway_call("team_mission.team_profile.get", params)
    result, error = _unwrap_response(response)
    if error:
        return tool_error(error)
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), Mapping) else {}
    return tool_result(
        success=True,
        mission_id=mission_id,
        binding=result.get("binding") if isinstance(result.get("binding"), Mapping) else {},
        snapshot=snapshot,
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
        conversation_session_id = _text(conversation.get("stable_session_id")) or conversation_id
    if not conversation_id or not conversation_session_id:
        return tool_error("Team Mission conversation context is not available for this Leader turn.")
    active_run_id = _active_run_id(parent_agent)
    members = list(team_context.get("members") or []) if isinstance(team_context.get("members"), list) else []
    metadata = {
        "conversation_id": conversation_id,
        "stableTeamSessionId": conversation_session_id,
        "started_from_leader_conversation_run_id": active_run_id,
        "task_id": task_id,
        "task_title": title,
        "task_objective": objective,
    }
    create_response = _gateway_call(
        "team_mission.create",
        {
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "team_id": _text(team_context.get("team_id") or team_context.get("teamId")),
            "title": title,
            "objective": objective,
            "mode": _text(args.get("mode") or team_context.get("mode")) or "supervised_mission",
            "workspace": {
                "workspace_id": _text(team_context.get("workspace_id") or team_context.get("workspaceId")),
                "workspace_path": _text(team_context.get("workspace_path") or team_context.get("workspacePath")),
            },
            "members": members,
            "team_capability": _team_capability_payload(team_context),
            "task_id": task_id,
            "record_user_task_message": False,
            "metadata": metadata,
        },
    )
    created, error = _unwrap_response(create_response)
    if error:
        return tool_error(error)
    graph = db.get_team_mission_graph(mission_id)
    started = created.get("leader_start") if isinstance(created.get("leader_start"), Mapping) else {}
    node = started.get("node") if isinstance(started.get("node"), Mapping) else _root_leader_node(graph)
    return tool_result(
        success=True,
        intent="start_team_task",
        mission_id=mission_id,
        conversation_id=conversation_id,
        task_id=task_id,
        node=node or {},
        run=started.get("run") if isinstance(started, Mapping) else {},
        graph_summary=_graph_summary(graph),
    )


registry.register(
    name="team_mission_status",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_status",
        "description": "Read the current Hermes Team Mission graph and memory summary for a Leader conversation answer.",
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
    name="team_mission_team_profile",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_team_profile",
        "description": (
            "Read the canonical Hermes Team Capability Snapshot bound to this Team Mission. "
            "Use this before planning when member capability, constraints, or assignment evidence is needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    handler=_handle_team_profile,
    emoji="",
)

registry.register(
    name="team_mission_start_task",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_start_task",
        "description": (
            "Start a new Hermes Team Mission planning task from the current Leader conversation. "
            "Use only when the user is clearly asking for a substantive executable team task."
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
