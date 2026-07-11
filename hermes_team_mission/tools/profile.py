"""Hermes Team Mission team/member profile inspection tool."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from hermes_team_mission.context.worker_context import TOOL_RESULT_BUDGET_CHARS
from hermes_team_mission.runtime.profile_scope import compact_team_profile_snapshot
from hermes_team_mission.runtime.profile_scope import gateway_call
from hermes_team_mission.runtime.profile_scope import metadata as _metadata
from hermes_team_mission.runtime.profile_scope import team_mission_control_db
from hermes_team_mission.runtime.profile_scope import text as _text
from hermes_team_mission.runtime.profile_scope import unwrap_response
from tools.registry import registry, tool_error, tool_result


_TOOLSET = "team_mission_read"
_LEADER_ROLES = {"leader", "root"}
_LEADER_NODE_KINDS = {"root"}


def _get_db(parent_agent=None):
    if _active_run_id({}, parent_agent):
        db = getattr(parent_agent, "_session_db", None) if parent_agent is not None else None
        if db is not None:
            return db
    return team_mission_control_db(parent_agent)


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


def _leader_team_context() -> dict[str, Any] | str:
    context = _session_context()
    team = context.get("team_mission") if isinstance(context.get("team_mission"), Mapping) else {}
    if _text(team.get("kind")) != "leader_conversation":
        return "This tool can only use Leader conversation context from a Team Mission Leader turn."
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


def _profile_request_options(args: Mapping[str, Any]) -> dict[str, Any]:
    raw_member_ids = args.get("member_ids") or args.get("memberIds") or args.get("member_id") or args.get("memberId")
    if isinstance(raw_member_ids, str):
        member_ids = {_text(item) for item in raw_member_ids.replace(",", "\n").split("\n") if _text(item)}
    elif isinstance(raw_member_ids, (list, tuple, set)):
        member_ids = {_text(item) for item in raw_member_ids if _text(item)}
    else:
        member_ids = set()
    try:
        limit = int(args.get("limit") or 12)
    except Exception:
        limit = 12
    return {
        "detail": _text(args.get("detail")) or "assignment",
        "member_ids": member_ids,
        "limit": limit,
    }


def _leader_run_context(args: dict[str, Any], parent_agent=None) -> tuple[Any, str, dict[str, Any], dict[str, Any], dict[str, Any]] | str:
    db = _get_db(parent_agent)
    if db is None:
        return "Session database is not available."
    run_id = _active_run_id(args, parent_agent)
    if not run_id:
        return "run_id is required for Team Mission Leader tools."
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
    if binding_role not in _LEADER_ROLES and actor not in _LEADER_ROLES and node_kind not in _LEADER_NODE_KINDS:
        return "Only a Team Mission Leader run can read the Team Capability Snapshot."
    return db, run_id, binding, mission, node


def _leader_profile_params(team_context: dict[str, Any], mission_id: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    team_id = _text(team_context.get("team_id") or team_context.get("teamId"))
    conversation_id = _text(team_context.get("conversation_id") or team_context.get("conversationId"))
    conversation_session_id = _text(team_context.get("conversation_session_id") or team_context.get("conversationSessionId"))
    if team_id:
        params["team_id"] = team_id
    if conversation_id:
        params["conversation_id"] = conversation_id
    if conversation_session_id:
        params["conversation_session_id"] = conversation_session_id
    if mission_id:
        params["mission_id"] = mission_id
    snapshot_id = _text(team_context.get("team_capability_snapshot_id") or team_context.get("teamCapabilitySnapshotId"))
    if snapshot_id:
        params["snapshot_id"] = snapshot_id
    return params


def _snapshot_id_from_mission(mission: Mapping[str, Any]) -> str:
    mission_metadata = _metadata(mission.get("metadata"))
    snapshot_meta = mission_metadata.get("team_capability_snapshot")
    if isinstance(snapshot_meta, Mapping):
        return _text(snapshot_meta.get("snapshot_id") or snapshot_meta.get("snapshotId"))
    return _text(mission_metadata.get("team_capability_snapshot_id") or mission_metadata.get("teamCapabilitySnapshotId"))


def _leader_run_profile_params(
    mission_id: str,
    mission: Mapping[str, Any],
    node: Mapping[str, Any],
) -> dict[str, Any]:
    """ADR-0001 Phase 2.D params for the planning-leader profile RPC.

    Carries canonical identity and auxiliary hints so the control-plane handler
    can resolve the snapshot without the worker touching DB read-model methods.
    """
    params: dict[str, Any] = {"mission_id": mission_id}
    snapshot_id = _snapshot_id_from_mission(mission)
    if snapshot_id:
        params["snapshot_id"] = snapshot_id
    team_id = _text(mission.get("team_id"))
    if team_id:
        params["team_id"] = team_id
    mission_metadata = _metadata(mission.get("metadata"))
    mission_members = mission_metadata.get("members")
    if isinstance(mission_members, list) and mission_members:
        params["mission_metadata"] = {"members": mission_members}
        title = _text(mission.get("title"))
        objective = _text(mission.get("objective"))
        mode = _text(mission.get("mode") or mission_metadata.get("mode_strategy"))
        if title:
            params["mission_title"] = title
        if objective:
            params["mission_objective"] = objective
        if mode:
            params["mission_mode"] = mode
    node_id = _text(node.get("node_id"))
    if node_id:
        params["node_id"] = node_id
    return params


def _handle_leader_team_profile(args: dict[str, Any], parent_agent=None) -> str:
    options = _profile_request_options(args if isinstance(args, Mapping) else {})
    ctx = _leader_team_context()
    if isinstance(ctx, str):
        return tool_error(ctx)
    team_context = ctx
    db = _get_db(parent_agent)
    mission_id, _graph = _active_mission_graph(db, team_context)
    response = gateway_call(
        "team_mission.team_profile.get",
        _leader_profile_params(team_context, mission_id),
        db=db,
    )
    result, error = unwrap_response(response)
    if error:
        return tool_error(error)
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), Mapping) else {}
    return tool_result(
        success=True,
        mission_id=mission_id,
        source=_text(result.get("source")),
        binding=result.get("binding") if isinstance(result.get("binding"), Mapping) else {},
        snapshot=compact_team_profile_snapshot(snapshot, **options),
    )


def _handle_leader_run_team_profile(args: dict[str, Any], parent_agent=None) -> str:
    options = _profile_request_options(args if isinstance(args, Mapping) else {})
    ctx = _leader_run_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    _db, _run_id, binding, mission, node = ctx
    mission_id = _text(binding.get("mission_id") or mission.get("mission_id"))
    # ADR-0001 Phase 2.D: route through the control-plane RPC instead of direct
    # DB calls. Worker DB proxy does not expose snapshot read-model methods;
    # team_mission.team_profile.get is the read model boundary.
    response = gateway_call(
        "team_mission.team_profile.get",
        _leader_run_profile_params(mission_id, mission, node),
        db=_db,
    )
    result, error = unwrap_response(response)
    if error:
        return tool_error(error)
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), Mapping) else {}
    return tool_result(
        success=True,
        mission_id=mission_id,
        source=_text(result.get("source")),
        binding=result.get("binding") if isinstance(result.get("binding"), Mapping) else {},
        node={
            "node_id": _text(node.get("node_id")),
            "kind": _text(node.get("kind")),
            "role": _text(_metadata(node.get("metadata")).get("role") or node.get("kind")),
            "phase": _text(_metadata(node.get("metadata")).get("phase") or mission.get("status")),
        },
        snapshot=compact_team_profile_snapshot(snapshot, **options),
    )


def _handle_team_profile(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    leader_ctx = _leader_team_context()
    if not isinstance(leader_ctx, str):
        return _handle_leader_team_profile(args, parent_agent)
    leader_run_ctx = _leader_run_context(args, parent_agent)
    if not isinstance(leader_run_ctx, str):
        return _handle_leader_run_team_profile(args, parent_agent)
    return tool_error(
        "Team Mission profile context is not available. "
        f"Leader context: {leader_ctx} Leader run context: {leader_run_ctx}"
    )


registry.register(
    name="team_mission_team_profile",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_team_profile",
        "description": (
            "Read the canonical Hermes Team Capability Snapshot for the current Team Mission. "
            "Use this before deciding assignees, planning graph nodes, or answering member capability questions. "
            "Available to Team Mission Leader conversation turns and Team Mission Leader node runs in any phase."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "string",
                    "description": "Profile detail level: compact, assignment, or full. Defaults to assignment. Use full only for a specific member/page.",
                    "enum": ["compact", "assignment", "full"],
                },
                "member_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 120},
                    "description": "Optional member ids to inspect. Leave empty for the assignment overview.",
                    "maxItems": 12,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum members to return. Defaults to 12, hard capped at 50.",
                },
            },
            "required": [],
        },
    },
    handler=_handle_team_profile,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)


__all__ = [name for name in globals() if not name.startswith("__")]
