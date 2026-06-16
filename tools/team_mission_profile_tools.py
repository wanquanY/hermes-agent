"""Hermes Team Mission team/member profile inspection tool."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from hermes_state import SessionDB
from hermes_team_mission_assignees import normalized_member_dicts
from hermes_team_mission_profile_tools import compact_team_profile_snapshot
from hermes_team_mission_profile_tools import gateway_call
from hermes_team_mission_profile_tools import metadata as _metadata
from hermes_team_mission_profile_tools import text as _text
from hermes_team_mission_profile_tools import unwrap_response
from tools.registry import registry, tool_error, tool_result


_TOOLSET = "team_mission_read"
_LEADER_ROLES = {"leader", "root"}
_LEADER_NODE_KINDS = {"root"}


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


def _fallback_snapshot_from_mission(mission: Mapping[str, Any]) -> dict[str, Any]:
    mission_metadata = _metadata(mission.get("metadata"))
    raw_members = mission_metadata.get("members") if isinstance(mission_metadata.get("members"), list) else []
    members = normalized_member_dicts(raw_members)
    member_profiles: list[dict[str, Any]] = []
    for member in members:
        member_profile = {
            "member_id": _text(member.get("member_id")),
            "agent_profile_id": _text(member.get("profile_id") or member.get("agent_profile_id")),
            "display_name": _text(member.get("display_name") or member.get("name")),
            "role": _text(member.get("role")),
            "profile_description": _text(member.get("profile_summary") or member.get("profile_description")),
            "capability_tags": list(member.get("capability_tags") or []),
            "default_toolsets": list(member.get("default_toolsets") or []),
            "recommended_skills": list(member.get("recommended_skills") or []),
            "strengths": list(member.get("strengths") or []),
            "limitations": list(member.get("limitations") or []),
            "best_for_tasks": list(member.get("best_for_tasks") or []),
            "avoid_tasks": list(member.get("avoid_tasks") or []),
            "radar_scores": list(member.get("radar_scores") or []),
        }
        member_profiles.append({key: value for key, value in member_profile.items() if value not in ("", [], {})})
    return {
        "snapshot_id": _snapshot_id_from_mission(mission),
        "team_id": _text(mission.get("team_id")),
        "status": "mission_metadata",
        "team_profile": {
            "display_name": _text(mission_metadata.get("team_name") or mission.get("title")),
            "collaboration_mode": _text(mission.get("mode")),
            "positioning": "Fallback Team Mission member roster from mission metadata.",
        },
        "member_profiles": member_profiles,
    }


def _resolve_leader_run_profile(db, mission: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    mission_id = _text(mission.get("mission_id"))
    snapshot_id = _snapshot_id_from_mission(mission)
    snapshot = db.get_team_capability_snapshot(snapshot_id) if snapshot_id else {}
    binding = db.get_team_capability_snapshot_binding(mission_id) if mission_id else {}
    source = "snapshot_id" if snapshot else ""
    if not snapshot and mission_id:
        snapshot = db.get_bound_team_capability_snapshot(mission_id)
        if snapshot:
            source = "mission_binding"
    if not snapshot:
        team_id = _text(mission.get("team_id"))
        snapshot = db.get_latest_team_capability_snapshot(team_id) if team_id else {}
        if snapshot:
            source = "latest_team_snapshot"
    if snapshot:
        return snapshot, binding, source
    return _fallback_snapshot_from_mission(mission), binding, "mission_metadata_members"


def _handle_leader_team_profile(args: dict[str, Any], parent_agent=None) -> str:
    del args
    ctx = _leader_team_context()
    if isinstance(ctx, str):
        return tool_error(ctx)
    team_context = ctx
    db = _get_db(parent_agent)
    mission_id, _graph = _active_mission_graph(db, team_context)
    response = gateway_call("team_mission.team_profile.get", _leader_profile_params(team_context, mission_id))
    result, error = unwrap_response(response)
    if error:
        return tool_error(error)
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), Mapping) else {}
    return tool_result(
        success=True,
        mission_id=mission_id,
        source=_text(result.get("source")),
        binding=result.get("binding") if isinstance(result.get("binding"), Mapping) else {},
        snapshot=compact_team_profile_snapshot(snapshot),
    )


def _handle_leader_run_team_profile(args: dict[str, Any], parent_agent=None) -> str:
    ctx = _leader_run_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, _run_id, binding, mission, node = ctx
    mission_id = _text(binding.get("mission_id") or mission.get("mission_id"))
    snapshot, snapshot_binding, source = _resolve_leader_run_profile(db, mission)
    return tool_result(
        success=True,
        mission_id=mission_id,
        source=source,
        binding=snapshot_binding,
        node={
            "node_id": _text(node.get("node_id")),
            "kind": _text(node.get("kind")),
            "role": _text(_metadata(node.get("metadata")).get("role") or node.get("kind")),
            "phase": _text(_metadata(node.get("metadata")).get("phase") or mission.get("status")),
        },
        snapshot=compact_team_profile_snapshot(snapshot),
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
            "properties": {},
            "required": [],
        },
    },
    handler=_handle_team_profile,
    emoji="",
)
