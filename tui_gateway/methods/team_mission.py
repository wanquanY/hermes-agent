# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_team_leader_runtime_context import resolve_team_leader_runtime_params, resolve_team_runtime_members
from hermes_team_mission_artifact_refs import artifact_refs_from_payload
from hermes_team_mission_conversation_state import is_placeholder_team_mission_conversation_title as _is_placeholder_team_mission_conversation_title
from hermes_team_mission_conversation_utils import append_user_task_message as _append_team_user_task_message
from hermes_team_mission_conversation_utils import conversation_session_id as _team_conversation_session_id
from hermes_team_mission_modes import MODE_AUTONOMOUS_MISSION
from hermes_team_mission_modes import MODE_SUPERVISED_MISSION
from hermes_team_mission_modes import strategy_for_mode
from hermes_team_mission_profile_tools import team_mission_control_db as _team_mission_control_db
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.methods.team_registry import _team_for_projection as _registry_team_for_projection
from tui_gateway.services.artifacts import delete_session_artifacts
from tui_gateway.services import run_control
from tui_gateway.services.profile_context import enter_profile_context as _enter_profile_context_for_team
from tui_gateway.services.profile_context import leave_profile_context as _leave_profile_context_for_team
from tui_gateway.services.profile_context import profile_context_for_params as _profile_context_for_params
from tui_gateway.services.prompt_attachments import submitted_attachments as _submitted_attachments
from tui_gateway.services.team_mission_conversation_recovery import recover_conversation_active_run
from tui_gateway.services.team_mission_leader_runs import ensure_team_leader_message_run_state
from tui_gateway.services.team_mission_workspace import (
    bind_team_mission_session_workspace,
    resolve_team_mission_workspace_context,
    workspace_id_from_params as _team_workspace_id_from_params,
    workspace_path_from_params as _team_workspace_path_from_params,
    workspace_payload_from_params as _team_workspace_payload_from_params,
)
from tui_gateway.services.team_mission_scheduler import TeamMissionReadyScheduler
from tui_gateway.services.workspace import delete_session_workspace_bindings

_server = bind_server_globals(globals())
_log = logging.getLogger(__name__)


def _get_db():
    return _team_mission_control_db()


def _get_runtime_db():
    try:
        return _server._get_db()
    except Exception:
        return None


def _team_detail_projection_for_conversation(db, conversation: dict | None = None, mission: dict | None = None) -> dict:
    conversation = conversation if isinstance(conversation, dict) else {}
    mission = mission if isinstance(mission, dict) else {}
    team_id = str(
        conversation.get("team_id")
        or conversation.get("teamId")
        or mission.get("team_id")
        or mission.get("teamId")
        or ""
    ).strip()
    if not team_id:
        return {}
    if not hasattr(db, "get_agent_team_with_members") and not hasattr(db, "get_agent_team"):
        return {}
    team = db.get_agent_team_with_members(team_id) if hasattr(db, "get_agent_team_with_members") else db.get_agent_team(team_id)
    if not isinstance(team, dict) or not team:
        return {}
    members = team.get("members") if isinstance(team.get("members"), list) else []
    if not members and hasattr(db, "list_agent_team_members"):
        members = db.list_agent_team_members(team_id)
    return _registry_team_for_projection(
        team,
        members=members if isinstance(members, list) else [],
        projection="detail",
        include_members=True,
    )


def _attach_team_detail_projection(db, result: dict) -> dict:
    if not isinstance(result, dict) or not result:
        return result
    conversation = result.get("conversation") if isinstance(result.get("conversation"), dict) else {}
    mission = result.get("mission") if isinstance(result.get("mission"), dict) else {}
    team = _team_detail_projection_for_conversation(db, conversation, mission)
    if not team:
        return result
    graph = result.get("graph") if isinstance(result.get("graph"), dict) else {}
    return {
        **result,
        "team": team,
        "graph": {
            **graph,
            "team": team,
        } if graph else graph,
    }


def _conversation_title_from_submit(db, params: dict, text: str) -> str:
    message_title = str(
        params.get("persist_user_message")
        or params.get("persistUserMessage")
        or params.get("draft_text")
        or params.get("draftText")
        or text
        or ""
    ).strip()
    message_title = " ".join(message_title.split())
    if not message_title:
        return ""
    try:
        return db.sanitize_title(message_title[:100].rstrip()) or ""
    except Exception:
        return ""


def _ensure_team_mission_runtime_session_shell(stable_session_id: str) -> str:
    stable_session_id = str(stable_session_id or "").strip()
    if not stable_session_id or not os.getenv("DOVIE_HERMES_CONTROL_HOME"):
        return ""
    runtime_db = _get_runtime_db()
    if runtime_db is None:
        return "team mission runtime state db unavailable"
    try:
        if not runtime_db.get_session(stable_session_id):
            runtime_db.create_session(stable_session_id, source="team_mission", transient=False)
    except Exception:
        return "team mission runtime session shell unavailable"
    return ""


_TEAM_LEADER_TOOLSET_SCOPE = "exact"
_TEAM_LEADER_CONVERSATION_TOOLSETS = (
    "team_mission_conversation_leader",
    "clarify",
    "vision",
    "file",
    "terminal",
    "todo",
)
_TEAM_LEADER_DISABLED_TOOLSETS = ("delegation",)
_TEAM_LEADER_BLOCKED_TOOLS = ("delegate_task",)
_TEAM_LEADER_DIRECT_REPLY_REASONING_CONFIG = {"enabled": False}
_TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS = (
    "不要启动团队任务",
    "不要发起团队任务",
    "不要创建团队任务",
    "不要启动任务",
    "不要发起任务",
    "不要创建任务",
    "不需要团队任务",
    "无需团队任务",
    "别启动团队任务",
    "别发起团队任务",
    "do not start a team mission",
    "don't start a team mission",
    "do not start team mission",
    "don't start team mission",
    "do not launch a team mission",
    "don't launch a team mission",
    "do not start a team task",
    "don't start a team task",
)
_TEAM_LEADER_DIRECT_REPLY_SELF_MARKERS = (
    "自己完成",
    "你自己完成",
    "你来完成",
    "leader自己完成",
    "leader 直接完成",
    "answer directly",
    "reply directly",
)
_TEAM_LEADER_DIRECT_REPLY_NEGATED_SELF_MARKERS = (
    "不要直接自己完成",
    "不要自己完成",
    "别自己完成",
    "不要你自己完成",
    "别你自己完成",
    "不要你来完成",
    "别你来完成",
    "do not answer directly",
    "don't answer directly",
    "do not reply directly",
    "don't reply directly",
)
_TEAM_LEADER_START_TASK_MARKERS = (
    "启动团队任务",
    "发起团队任务",
    "创建团队任务",
    "执行团队任务",
    "开始团队任务",
    "启动一个团队任务",
    "发起一个团队任务",
    "创建一个团队任务",
    "执行一个团队任务",
    "让团队",
    "团队来",
    "团队执行",
    "团队协作",
    "任务图",
    "成员节点",
    "汇总节点",
    "start a team mission",
    "launch a team mission",
    "create a team mission",
    "run a team mission",
    "start a team task",
    "launch a team task",
    "create a team task",
    "run a team task",
)


def _mission_id_from_params(params: dict) -> str:
    return str(
        params.get("mission_id")
        or params.get("missionId")
        or ""
    ).strip()


def _graph_mission_ids(graph: dict) -> set[str]:
    graph = graph if isinstance(graph, dict) else {}
    mission_ids: set[str] = set()
    mission = graph.get("mission")
    if isinstance(mission, dict):
        mission_id = str(
            mission.get("mission_id")
            or mission.get("missionId")
            or ""
        ).strip()
        if mission_id:
            mission_ids.add(mission_id)
    for frame in graph.get("task_frames") or graph.get("taskFrames") or []:
        if not isinstance(frame, dict):
            continue
        mission_id = str(
            frame.get("missionId")
            or frame.get("mission_id")
            or ""
        ).strip()
        if mission_id:
            mission_ids.add(mission_id)
    return mission_ids


def _bounded_limit(value, default: int = 2000, maximum: int = 10000) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _bounded_byte_limit(value, default: int = 4 * 1024 * 1024, maximum: int = 16 * 1024 * 1024) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(64 * 1024, min(parsed, maximum))


def _event_json_size(event: dict) -> int:
    try:
        return len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return len(str(event).encode("utf-8"))


def _event_seq(event: dict) -> int:
    try:
        return int(event.get("seq") or 0)
    except Exception:
        return 0


def _team_mission_event_page(
    events: list[dict],
    *,
    after_seq: int,
    limit: int,
    byte_limit: int,
) -> tuple[list[dict], bool, int]:
    by_seq = {
        _event_seq(event): event
        for event in events
        if isinstance(event, dict) and _event_seq(event) > after_seq
    }
    ordered_events = [by_seq[seq] for seq in sorted(by_seq)]
    page: list[dict] = []
    total_bytes = 0
    has_more = False
    for event in ordered_events:
        event_bytes = _event_json_size(event)
        if len(page) >= limit:
            has_more = True
            break
        if page and total_bytes + event_bytes > byte_limit:
            has_more = True
            break
        page.append(event)
        total_bytes += event_bytes
    if len(page) < len(ordered_events):
        has_more = True
    return page, has_more, total_bytes


def _run_id_from_params(params: dict) -> str:
    return str(params.get("run_id") or params.get("runId") or "").strip()


def _node_payload_from_params(params: dict) -> dict:
    node = params.get("node")
    if isinstance(node, dict):
        return node
    return params


def _node_id_from_params(params: dict) -> str:
    payload = _node_payload_from_params(params)
    return str(payload.get("node_id") or payload.get("nodeId") or payload.get("id") or "").strip()


def _default_node_session_id(mission_id: str, node_id: str) -> str:
    safe_node_id = str(node_id or "").replace(":", "_")
    return f"team:{mission_id}:node:{safe_node_id}"


def _edge_payload_from_params(params: dict) -> dict:
    edge = params.get("edge")
    if isinstance(edge, dict):
        return edge
    return params


def _workspace_payload(params: dict) -> dict:
    return _team_workspace_payload_from_params(params)


def _workspace_id_from_params(params: dict) -> str:
    return _team_workspace_id_from_params(params)


def _workspace_path_from_params(params: dict) -> str:
    return _team_workspace_path_from_params(params)


def _team_capability_payload(params: dict) -> dict:
    raw = (
        params.get("team_capability")
        or params.get("teamCapability")
        or params.get("team_capability_snapshot")
        or params.get("teamCapabilitySnapshot")
        or params.get("capability_snapshot")
        or params.get("capabilitySnapshot")
        or {}
    )
    return raw if isinstance(raw, dict) else {}


def _team_capability_snapshot_id(params: dict) -> str:
    payload = _team_capability_payload(params)
    return str(
        payload.get("snapshot_id")
        or payload.get("snapshotId")
        or params.get("snapshot_id")
        or params.get("snapshotId")
        or params.get("team_capability_snapshot_id")
        or params.get("teamCapabilitySnapshotId")
        or ""
    ).strip()


def _snapshot_binding_metadata(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict) or not snapshot:
        return {}
    return {
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "snapshot_version": int(snapshot.get("version") or 0),
        "source_digest": str(snapshot.get("source_digest") or ""),
        "status": str(snapshot.get("status") or ""),
    }


def _member_capability_by_id(snapshot: dict) -> dict[str, dict]:
    member_profiles = snapshot.get("member_profiles") if isinstance(snapshot.get("member_profiles"), list) else []
    result: dict[str, dict] = {}
    for profile in member_profiles:
        if not isinstance(profile, dict):
            continue
        member_id = str(profile.get("member_id") or "").strip()
        profile_id = str(profile.get("agent_profile_id") or "").strip()
        if member_id:
            result[member_id] = profile
        if profile_id:
            result.setdefault(profile_id, profile)
    return result


def _members_with_capability_snapshot(members: list, snapshot: dict) -> list:
    if not isinstance(snapshot, dict) or not snapshot:
        return members
    capabilities = _member_capability_by_id(snapshot)
    if not capabilities:
        return members
    enriched = []
    for item in members:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        member_id = str(item.get("member_id") or item.get("memberId") or item.get("id") or "").strip()
        profile_id = str(item.get("profile_id") or item.get("profileId") or item.get("agent_profile_id") or item.get("agentProfileId") or "").strip()
        capability = capabilities.get(member_id) or capabilities.get(profile_id) or {}
        if not capability:
            enriched.append(item)
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        enriched.append({
            **item,
            "capability_tags": item.get("capability_tags") or item.get("capabilityTags") or capability.get("capability_tags") or [],
            "profile_summary": capability.get("profile_description") or "",
            "best_for_tasks": capability.get("best_for_tasks") or [],
            "avoid_tasks": capability.get("avoid_tasks") or [],
            "strengths": capability.get("strengths") or [],
            "limitations": capability.get("limitations") or [],
            "default_toolsets": capability.get("default_toolsets") or [],
            "recommended_skills": capability.get("recommended_skills") or [],
            "radar_scores": capability.get("radar_scores") or [],
            "metadata": {
                **metadata,
                "team_capability_snapshot_id": snapshot.get("snapshot_id") or "",
                "team_capability_snapshot_version": snapshot.get("version") or 0,
            },
        })
    return enriched


def _resolve_team_capability_snapshot_for_params(db, params: dict, *, team_id: str = "") -> dict:
    snapshot_id = _team_capability_snapshot_id(params)
    if snapshot_id:
        return db.get_team_capability_snapshot(snapshot_id)
    resolved_team_id = str(team_id or params.get("team_id") or params.get("teamId") or "").strip()
    if not resolved_team_id:
        return {}
    return _resolve_team_capability_snapshot_from_registry(db, params, team_id=resolved_team_id)


def _resolve_team_capability_snapshot_from_registry(
    db,
    params: dict,
    *,
    team_id: str = "",
    force_refresh: bool = False,
) -> dict:
    resolved_team_id = str(team_id or params.get("team_id") or params.get("teamId") or "").strip()
    if not resolved_team_id:
        return {}
    registry_payload = _team_capability_registry_payload(db, params, team_id=resolved_team_id)
    return db.resolve_team_capability_snapshot(
        team_id=resolved_team_id,
        source_packet=registry_payload,
        force_refresh=force_refresh,
    )


def _team_id_for_profile(params: dict, *, mission: dict | None = None, conversation: dict | None = None) -> str:
    mission = mission if isinstance(mission, dict) else {}
    conversation = conversation if isinstance(conversation, dict) else {}
    return str(
        params.get("team_id")
        or params.get("teamId")
        or mission.get("team_id")
        or mission.get("teamId")
        or conversation.get("team_id")
        or conversation.get("teamId")
        or ""
    ).strip()


def _archived_team_write_error(db, team_id: str) -> str:
    resolved_team_id = str(team_id or "").strip()
    if not resolved_team_id or not hasattr(db, "get_agent_team"):
        return ""
    try:
        team = db.get_agent_team(resolved_team_id)
    except Exception:
        return ""
    if isinstance(team, dict) and str(team.get("status") or "").strip().lower() == "archived":
        return f"team archived: {resolved_team_id}"
    return ""


def _team_default_mode(db, team_id: str) -> str:
    resolved_team_id = str(team_id or "").strip()
    if not resolved_team_id or not hasattr(db, "get_agent_team"):
        return ""
    team = db.get_agent_team(resolved_team_id)
    if not isinstance(team, dict) or not team:
        return ""
    return str(team.get("default_mode") or team.get("defaultMode") or "").strip()


def _resolve_team_mission_create_mode(db, params: dict, *, team_id: str) -> str:
    requested_mode = str(params.get("mode") or "").strip()
    default_mode = _team_default_mode(db, team_id)
    if requested_mode == MODE_AUTONOMOUS_MISSION and default_mode == MODE_SUPERVISED_MISSION:
        raise ValueError(
            "team policy requires supervised_mission; autonomous_mission cannot be selected by request payload"
        )
    return requested_mode or default_mode or MODE_SUPERVISED_MISSION


def _text_list(value) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = []
    result = []
    seen = set()
    for item in raw_items:
        normalized = str(item or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _profile_id_from_team_member(member: dict) -> str:
    return str(
        member.get("agent_profile_id")
        or member.get("agentProfileId")
        or member.get("profile_id")
        or member.get("profileId")
        or ""
    ).strip()


def _profile_for_team_member(db, member: dict) -> dict:
    profile_id = _profile_id_from_team_member(member)
    if not profile_id or not hasattr(db, "get_agent_profile"):
        return {}
    profile = db.get_agent_profile(profile_id)
    return profile if isinstance(profile, dict) else {}


def _team_capability_registry_payload(db, params: dict, *, team_id: str = "") -> dict:
    resolved_team_id = str(team_id or params.get("team_id") or params.get("teamId") or "").strip()
    if not resolved_team_id:
        raise ValueError("team_id required")
    if not hasattr(db, "get_agent_team"):
        raise ValueError("team registry unavailable")
    team = db.get_agent_team(resolved_team_id)
    if not isinstance(team, dict) or not team:
        raise ValueError(f"team not found: {resolved_team_id}")
    runtime_members = resolve_team_runtime_members({"team_id": resolved_team_id}, db=db)
    members = []
    for member in runtime_members:
        if not isinstance(member, dict):
            continue
        profile = _profile_for_team_member(db, member)
        dovie_profile = member.get("dovie_profile") if isinstance(member.get("dovie_profile"), dict) else {}
        members.append({
            "memberId": str(member.get("member_id") or member.get("memberId") or member.get("id") or "").strip(),
            "agentProfileId": _profile_id_from_team_member(member),
            "agentProfileVersionId": str(
                member.get("agent_profile_version_id")
                or member.get("agentProfileVersionId")
                or profile.get("agent_profile_version_id")
                or profile.get("agentProfileVersionId")
                or profile.get("current_version_id")
                or profile.get("currentVersionId")
                or ""
            ).strip(),
            "displayName": str(
                profile.get("name")
                or dovie_profile.get("name")
                or member.get("display_name")
                or member.get("displayName")
                or ""
            ).strip(),
            "avatar": str(profile.get("avatar") or dovie_profile.get("avatar") or "").strip(),
            "role": str(member.get("role") or "member").strip(),
            "capabilityTags": _text_list(member.get("capability_tags") or member.get("capabilityTags")),
            "autoAssignable": member.get("auto_assignable", member.get("autoAssignable", True)) is not False,
            "maxConcurrentNodes": max(1, int(member.get("max_concurrent_nodes") or member.get("maxConcurrentNodes") or 1)),
            "permissionMode": str(member.get("permission_mode") or member.get("permissionMode") or "").strip(),
            "profile": {
                "description": str(profile.get("description") or "").strip(),
                "category": str(profile.get("category") or "").strip(),
                "tags": _text_list(profile.get("tags")),
                "defaultToolsets": _text_list(
                    profile.get("defaultToolsets")
                    or profile.get("default_toolsets")
                    or member.get("defaultToolsets")
                    or member.get("default_toolsets")
                ),
                "recommendedSkills": _text_list(
                    profile.get("recommendedSkills")
                    or profile.get("recommended_skills")
                    or member.get("recommendedSkills")
                    or member.get("recommended_skills")
                ),
            },
        })
    return {
        "teamId": resolved_team_id,
        "teamRevision": str(team.get("updated_at") or team.get("updatedAt") or ""),
        "team": {
            "name": str(team.get("name") or "").strip(),
            "description": str(team.get("description") or "").strip(),
            "defaultMode": str(team.get("default_mode") or team.get("defaultMode") or "").strip(),
            "policy": team.get("policy") if isinstance(team.get("policy"), dict) else {},
        },
        "members": members,
    }


def _team_runtime_members_from_registry(db, params: dict, *, mission: dict | None = None, team_id: str = "") -> list[dict]:
    seed = dict(params or {})
    if team_id:
        seed["team_id"] = team_id
        seed["teamId"] = team_id
    return resolve_team_runtime_members(seed, mission=mission, db=db)


def _bind_team_capability_snapshot_for_mission(db, *, mission_id: str, conversation_id: str, snapshot: dict) -> dict:
    snapshot_id = str((snapshot or {}).get("snapshot_id") or "").strip()
    if not snapshot_id:
        return {}
    return db.bind_team_capability_snapshot(
        mission_id=mission_id,
        conversation_id=conversation_id,
        snapshot_id=snapshot_id,
    )


def _conversation_session_id_from_params(params: dict, metadata: dict | None = None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("stable_team_session_id")
        or params.get("stableTeamSessionId")
        or params.get("team_session_id")
        or params.get("teamSessionId")
        or params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("stable_team_session_id")
        or metadata.get("stableTeamSessionId")
        or metadata.get("team_session_id")
        or metadata.get("teamSessionId")
        or ""
    ).strip()


def _conversation_id_from_params(params: dict, metadata: dict | None = None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(
        params.get("conversation_id")
        or params.get("conversationId")
        or params.get("team_conversation_id")
        or params.get("teamConversationId")
        or metadata.get("conversation_id")
        or metadata.get("conversationId")
        or metadata.get("team_conversation_id")
        or metadata.get("teamConversationId")
        or ""
    ).strip()


def _normalize_mission_metadata(params: dict, metadata: dict) -> dict:
    normalized = dict(metadata or {})
    conversation_id = _conversation_id_from_params(params, normalized)
    if conversation_id:
        normalized.setdefault("conversation_id", conversation_id)
        normalized.setdefault("conversationId", conversation_id)
    conversation_session_id = _conversation_session_id_from_params(params, normalized)
    if conversation_session_id:
        normalized.setdefault("conversation_session_id", conversation_session_id)
        normalized.setdefault("stableTeamSessionId", conversation_session_id)
    return normalized


def _normalize_toolsets(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _merge_toolsets(*values) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for name in _normalize_toolsets(value):
            if name not in seen:
                seen.add(name)
                merged.append(name)
    return merged


def _leader_disabled_toolsets(params: dict) -> list[str]:
    return _merge_toolsets(
        params.get("disabled_toolsets") or params.get("disabledToolsets"),
        _TEAM_LEADER_DISABLED_TOOLSETS,
    )


def _team_leader_tool_policy(*, surface: str) -> dict:
    return {
        "surface": surface,
        "role": "leader",
        "toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE,
        "disabled_toolsets": list(_TEAM_LEADER_DISABLED_TOOLSETS),
        "blocked_tools": list(_TEAM_LEADER_BLOCKED_TOOLS),
        "reason": "team_leader_control_plane",
    }


def _is_team_leader_control_node(node: dict) -> bool:
    return _node_role(node) in {"leader", "lead", "root"} or str(node.get("kind") or "").strip() == "root"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _conversation_only_from_params(params: dict) -> bool:
    return _truthy(
        params.get("conversation_only")
        or params.get("conversationOnly")
        or params.get("create_conversation_only")
        or params.get("createConversationOnly")
    )


def _falsey(value) -> bool:
    if isinstance(value, bool):
        return not value
    if value is None:
        return False
    return str(value).strip().lower() in {"0", "false", "no", "off"}


def _node_role(node: dict) -> str:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return str(metadata.get("role") or node.get("kind") or "worker").strip()


def _node_phase(node: dict) -> str:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return str(metadata.get("phase") or "").strip()


def _task_id_from_metadata(metadata: dict) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), dict) else {}
    return str(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
        or ""
    ).strip()


def _is_root_planning_node(node: dict) -> bool:
    return (
        isinstance(node, dict)
        and str(node.get("kind") or "") == "root"
        and _node_role(node) in {"leader", "lead", "root"}
        and _node_phase(node) in {"planning", "change_request", "discussion"}
    )


def _activate_mission_task(db, mission: dict, node: dict, *, source: str = "") -> dict:
    if not isinstance(mission, dict) or not mission or not _is_root_planning_node(node):
        return mission if isinstance(mission, dict) else {}
    metadata = dict(mission.get("metadata") or {})
    node_metadata = dict(node.get("metadata") or {})
    task_id = _task_id_from_metadata(node_metadata)
    title = str(node.get("title") or mission.get("title") or "").strip()
    objective = str(node.get("objective") or mission.get("objective") or title or "").strip()
    node_id = str(node.get("node_id") or "").strip()
    active_task = {
        "task_id": task_id,
        "title": title,
        "objective": objective,
        "root_node_id": node_id,
        "source": str(source or "").strip(),
    }
    next_metadata = {
        **metadata,
        "active_task": active_task,
        "active_task_id": task_id,
        "active_task_title": title,
        "active_task_objective": objective,
        "active_task_root_node_id": node_id,
    }
    if task_id:
        next_metadata.setdefault("task_id", task_id)
        next_metadata["activeTaskId"] = task_id
    updated = db.upsert_team_mission(
        mission_id=str(mission.get("mission_id") or ""),
        team_id=str(mission.get("team_id") or ""),
        title=title or str(mission.get("title") or ""),
        objective=objective or str(mission.get("objective") or ""),
        workspace_id=str(mission.get("workspace_id") or ""),
        workspace_path=str(mission.get("workspace_path") or ""),
        mode=str(mission.get("mode") or ""),
        status=str(mission.get("status") or "draft"),
        leader_session_id=str(mission.get("leader_session_id") or ""),
        metadata=next_metadata,
    )
    return updated if isinstance(updated, dict) and updated else mission


def _should_use_strategy_start_text(params: dict, mission: dict, node: dict) -> bool:
    if params.get("text") or params.get("prompt"):
        return False
    role = _node_role(node)
    phase = _node_phase(node)
    is_strategy_node = (
        role == "leader"
        and str(node.get("kind") or "") == "root"
        and phase in {"planning", "change_request", "discussion"}
        and bool(mission)
    )
    if bool(params.get("use_strategy_prompt") or params.get("useStrategyPrompt")):
        return is_strategy_node
    return is_strategy_node


def _strategy_start_text(params: dict, mission: dict, node: dict) -> str:
    explicit_text = str(params.get("text") or params.get("prompt") or "").strip()
    if explicit_text and not _should_use_strategy_start_text(params, mission, node):
        return explicit_text
    if _should_use_strategy_start_text(params, mission, node):
        strategy = strategy_for_mode(str(mission.get("mode") or "supervised_mission"))
        title = str(node.get("title") or mission.get("title") or "").strip()
        objective = str(node.get("objective") or mission.get("objective") or "").strip()
        return strategy.leader_start_text(
            mission_id=str(mission.get("mission_id") or ""),
            title=title,
            objective=objective,
            members=(mission.get("metadata") or {}).get("members") or (),
        )
    return str(explicit_text or node.get("objective") or node.get("title") or "").strip()


def _member_default_toolsets(member: dict) -> list[str]:
    if not isinstance(member, dict):
        return []
    profile = (
        member.get("dovie_profile")
        if isinstance(member.get("dovie_profile"), dict)
        else member.get("dovieProfile")
        if isinstance(member.get("dovieProfile"), dict)
        else {}
    )
    metadata = member.get("metadata") if isinstance(member.get("metadata"), dict) else {}
    return _normalize_toolsets(
        member.get("default_toolsets")
        or member.get("defaultToolsets")
        or profile.get("defaultToolsets")
        or profile.get("default_toolsets")
        or metadata.get("defaultToolsets")
        or metadata.get("default_toolsets")
    )


def _member_for_node(params: dict, mission: dict, node: dict, *, db=None) -> dict:
    for member in _leader_members_from_params(params, mission if isinstance(mission, dict) else {}, db=db):
        if _member_matches_node_profile(member, node):
            return member
    return {}


def _profile_current_toolsets(profile_params: dict) -> list[str]:
    context = _profile_context_for_params(profile_params)
    if not isinstance(context, dict):
        return []
    loader = globals().get("_load_enabled_toolsets")
    if not callable(loader):
        return []
    token = _enter_profile_context_for_team(context)
    try:
        return _normalize_toolsets(loader())
    except Exception:
        return []
    finally:
        _leave_profile_context_for_team(token)


def _start_toolsets(params: dict, mission: dict, node: dict, *, profile_params: dict | None = None, db=None) -> list[str]:
    if _is_team_leader_control_node(node):
        if _node_phase(node) in {"planning", "change_request"}:
            # Planning is "design the task graph", not "do the work". The leader is
            # allowed to (a) WRITE the graph (team_mission_planning), (b) ASK the user
            # for missing inputs (clarify) so it does not build the graph on guessed
            # assumptions and waste an approval+execution round, and (c) READ the
            # workspace (file_readonly) so the graph reflects what is actually there.
            # It is NOT allowed to write files or run commands — that is a worker job
            # behind the approval gate; giving leader write/exec here would bypass the
            # whole supervised approval boundary. The toolset_scope is "exact", so
            # everything must be listed explicitly.
            return ["team_mission_planning", "clarify", "file_readonly"]
        return ["team_mission_read"]
    toolsets = _normalize_toolsets(params.get("enabled_toolsets") or params.get("enabledToolsets"))
    if not toolsets:
        toolsets = _profile_current_toolsets(profile_params or {})
    if not toolsets:
        toolsets = _member_default_toolsets(_member_for_node(params, mission, node, db=db))
    if _should_use_strategy_start_text(params, mission, node) and _node_phase(node) in {"planning", "change_request"}:
        if "team_mission_planning" not in toolsets:
            toolsets.append("team_mission_planning")
    return toolsets


def _team_memory_disabled(params: dict, mission: dict) -> bool:
    if _truthy(params.get("disable_team_memory") or params.get("disableTeamMemory")):
        return True
    if _falsey(params.get("use_team_memory") if "use_team_memory" in params else params.get("useTeamMemory")):
        return True
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    policy = metadata.get("memory") if isinstance(metadata.get("memory"), dict) else {}
    if _truthy(policy.get("disabled")):
        return True
    return False


def _team_memory_include_team_scope(params: dict, mission: dict) -> bool:
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    policy = metadata.get("memory") if isinstance(metadata.get("memory"), dict) else {}
    explicit_scope = str(
        params.get("memory_scope")
        or params.get("memoryScope")
        or policy.get("scope")
        or ""
    ).strip().lower()
    if explicit_scope in {"team", "team_wide", "cross_conversation", "workspace"}:
        return True
    if explicit_scope in {"conversation", "session", "mission"}:
        return False
    return _truthy(
        params.get("include_team_memory")
        or params.get("includeTeamMemory")
        or params.get("cross_conversation_memory")
        or params.get("crossConversationMemory")
        or policy.get("include_team_memory")
        or policy.get("includeTeamMemory")
        or policy.get("cross_conversation")
        or policy.get("crossConversation")
    )


def _memory_items_from_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    memory = payload.get("memory_pack") or payload.get("memory_slice") or {}
    items = memory.get("items") if isinstance(memory, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _memory_context_text(*, label: str, payload: dict) -> str:
    items = _memory_items_from_payload(payload)
    if not items:
        return ""
    lines = [
        f"{label} (Hermes structured background; not new user input)",
        "Current user objective has highest priority. Use memory only as background, reusable artifacts, risks, and constraints. If memory conflicts with the current objective, surface the conflict and follow the current objective.",
        "",
        "Relevant memory:",
    ]
    for item in items[:12]:
        item_id = str(item.get("id") or "").strip()
        kind = str(item.get("kind") or "summary").strip()
        content = str(item.get("content") or "").strip()
        if len(content) > 700:
            content = content[:697].rstrip() + "..."
        sources = []
        source_nodes = item.get("source_node_ids") if isinstance(item.get("source_node_ids"), list) else []
        source_runs = item.get("source_run_ids") if isinstance(item.get("source_run_ids"), list) else []
        artifacts = item.get("artifact_refs") if isinstance(item.get("artifact_refs"), list) else []
        if source_nodes:
            sources.append("nodes=" + ",".join(str(node_id) for node_id in source_nodes[:4]))
        if source_runs:
            sources.append("runs=" + ",".join(str(run_id) for run_id in source_runs[:4]))
        if artifacts:
            artifact_uris = []
            for artifact in artifacts[:3]:
                if isinstance(artifact, dict):
                    artifact_uris.append(str(artifact.get("uri") or artifact.get("path") or artifact.get("id") or ""))
            artifact_uris = [uri for uri in artifact_uris if uri]
            if artifact_uris:
                sources.append("artifacts=" + ",".join(artifact_uris))
        source_text = f" Sources: {'; '.join(sources)}." if sources else ""
        id_text = f"{item_id} " if item_id else ""
        lines.append(f"- [{id_text}{kind}] {content}{source_text}")
    return "\n".join(lines).strip()


def _team_memory_for_node(db, params: dict, mission: dict, node: dict, *, objective: str) -> tuple[dict, str]:
    if _team_memory_disabled(params, mission):
        return {"disabled": True, "reason": "disabled_by_request_or_policy"}, ""
    mission_id = str(mission.get("mission_id") or "").strip()
    node_id = str(node.get("node_id") or "").strip()
    if not mission_id or not node_id:
        return {}, ""
    role = _node_role(node)
    kind = str(node.get("kind") or "").strip()
    phase = _node_phase(node)
    try:
        if role == "leader" and kind == "root" and phase in {"planning", "change_request", "discussion"}:
            payload = db.build_team_mission_memory_pack(
                mission_id=mission_id,
                objective=objective or str(mission.get("objective") or ""),
                workspace_id=str(mission.get("workspace_id") or ""),
                limit=int(params.get("memory_limit") or params.get("memoryLimit") or 8),
                include_team_scope=_team_memory_include_team_scope(params, mission),
            )
            text = _memory_context_text(label="Team Conversation Memory Pack", payload=payload)
            memory = payload.get("memory_pack") if isinstance(payload, dict) else {}
            return {
                "kind": "leader_memory_pack",
                "conversation_session_id": str(payload.get("conversation_session_id") or "") if isinstance(payload, dict) else "",
                "item_ids": list((memory or {}).get("item_ids") or []),
                "artifact_refs": list((memory or {}).get("artifact_refs") or []),
            }, text
        payload = db.build_team_mission_memory_slice(
            mission_id=mission_id,
            node_id=node_id,
            objective=objective or str(node.get("objective") or ""),
            limit=int(params.get("memory_limit") or params.get("memoryLimit") or 5),
            include_team_scope=_team_memory_include_team_scope(params, mission),
        )
        text = _memory_context_text(label="Team Conversation Memory Slice", payload=payload)
        memory = payload.get("memory_slice") if isinstance(payload, dict) else {}
        return {
            "kind": "worker_memory_slice",
            "conversation_session_id": str(payload.get("conversation_session_id") or "") if isinstance(payload, dict) else "",
            "item_ids": list((memory or {}).get("item_ids") or []),
            "artifact_refs": list((memory or {}).get("artifact_refs") or []),
            "dependency_node_ids": list((memory or {}).get("dependency_node_ids") or []),
        }, text
    except Exception as exc:
        return {"disabled": True, "reason": f"memory_build_failed: {exc}"}, ""


def _message_text_from_params(params: dict) -> str:
    return str(
        params.get("text")
        or params.get("message")
        or params.get("prompt")
        or params.get("objective")
        or ""
    ).strip()


def _root_leader_node(graph: dict) -> dict:
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        if str(node.get("kind") or "") == "root" and str(metadata.get("role") or "leader") in {"leader", "lead", "root"}:
            return node
    for node in nodes if isinstance(nodes, list) else []:
        if isinstance(node, dict) and str(node.get("kind") or "") == "root":
            return node
    return {}


def _leader_members_from_params(params: dict, mission: dict, *, db=None, strict: bool = False) -> list[dict]:
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    members = metadata.get("members") if isinstance(metadata.get("members"), list) else []
    resolved = [dict(item) for item in members if isinstance(item, dict)]
    if resolved:
        return resolved
    try:
        return resolve_team_runtime_members(params, mission=mission if isinstance(mission, dict) else {}, db=db)
    except ValueError:
        if strict:
            raise
    return []


def _recover_conversation_active_run(db, conversation: dict | None) -> None:
    recover_conversation_active_run(
        db=db,
        conversation=conversation,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    )


_TEAM_MISSION_ACTIVE_STATUSES = {
    "planning",
    "running",
    "partially_blocked",
    "blocked",
    "verifying",
    "waiting_approval",
}
_TEAM_MISSION_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}
_TEAM_MISSION_ACTIVE_NODE_STATUSES = {"starting", "running", "waiting_approval"}
_TEAM_MISSION_ACTIVE_RUN_STATUSES = {
    "queued",
    "starting",
    "running",
    "waiting_approval",
    "cancelling",
    "finalizing",
}


def _active_node_run_from_bindings(db, bindings: list[dict] | None) -> dict:
    active_run = {}
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        run_id = str(binding.get("run_id") or binding.get("runId") or "").strip()
        if not run_id:
            continue
        run = run_control.get_run(run_id, db=db) or {}
        if str(run.get("status") or "").strip() not in _TEAM_MISSION_ACTIVE_RUN_STATUSES:
            continue
        merged_run = {
            **binding,
            **run,
            "run_id": run_id,
            "runtime_session_id": str(run.get("runtime_session_id") or binding.get("runtime_session_id") or ""),
            "runtime_scope_key": str(run.get("runtime_scope_key") or binding.get("runtime_scope_key") or ""),
            "turn_id": str(run.get("turn_id") or binding.get("turn_id") or ""),
        }
        if not active_run or float(merged_run.get("updated_at") or 0) >= float(active_run.get("updated_at") or 0):
            active_run = merged_run
    return active_run


def _conversation_runtime_projection(db, conversation: dict | None) -> dict:
    if not isinstance(conversation, dict):
        return {}
    stable_session_id = str(
        conversation.get("stable_session_id")
        or conversation.get("stableSessionId")
        or ""
    ).strip()
    conversation_id = str(
        conversation.get("conversation_id")
        or conversation.get("conversationId")
        or ""
    ).strip()
    mission_id = str(
        conversation.get("active_mission_id")
        or conversation.get("activeMissionId")
        or conversation.get("mission_id")
        or conversation.get("missionId")
        or ""
    ).strip()
    runtime_summary = {}
    summary_fn = getattr(db, "get_team_mission_conversation_runtime_summary", None)
    if callable(summary_fn) and conversation_id:
        runtime_summary = summary_fn(conversation_id) or {}
    if isinstance(runtime_summary, dict):
        mission_id = str(runtime_summary.get("active_mission_id") or mission_id or "").strip()
    graph = db.get_team_mission_graph(mission_id) if mission_id and not runtime_summary else {}
    mission = (
        runtime_summary.get("mission") if isinstance(runtime_summary, dict) else {}
    ) or (graph.get("mission") if isinstance(graph, dict) else {})
    mission = mission if isinstance(mission, dict) else {}
    mission_status = str(mission.get("status") or conversation.get("status") or "").strip()
    if isinstance(runtime_summary, dict) and runtime_summary.get("mission_status"):
        mission_status = str(runtime_summary.get("mission_status") or "").strip()
    nodes = [node for node in (graph.get("nodes") if isinstance(graph, dict) else []) or [] if isinstance(node, dict)]
    active_nodes = [
        node for node in nodes
        if str(node.get("status") or "").strip() in _TEAM_MISSION_ACTIVE_NODE_STATUSES
    ]
    active_node_count = int(runtime_summary.get("active_node_count") or 0) if isinstance(runtime_summary, dict) else len(active_nodes)
    active_node_run = _active_node_run_from_bindings(
        db,
        runtime_summary.get("run_bindings") if isinstance(runtime_summary, dict) else [],
    )
    if active_node_run and active_node_count <= 0:
        active_node_count = 1
    pending_approvals = [
        item for item in (
            runtime_summary.get("pending_approvals")
            if isinstance(runtime_summary, dict)
            else []
        ) or []
        if isinstance(item, dict)
    ]
    approval_waiting = bool(pending_approvals) or any(
        str(node.get("kind") or "").strip() == "approval_gate"
        and str(node.get("status") or "").strip() == "waiting_approval"
        for node in nodes
    )
    run_state = run_control.session_status(
        stable_session_id,
        db=db,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    ) if stable_session_id else {}
    leader_running = bool(run_state.get("running"))
    node_running = bool(active_node_run)
    mission_running = bool(active_node_count) or mission_status in _TEAM_MISSION_ACTIVE_STATUSES
    terminal = mission_status in _TEAM_MISSION_TERMINAL_STATUSES
    running = bool(leader_running or node_running or (mission_running and not terminal))
    waiting_approval = approval_waiting or mission_status == "waiting_approval"
    active_run_id = str(run_state.get("active_run_id") or "") if leader_running else str(active_node_run.get("run_id") or "")
    active_turn_id = str(run_state.get("active_turn_id") or "") if leader_running else str(active_node_run.get("turn_id") or "")
    active_runtime_session_id = (
        str(run_state.get("active_runtime_session_id") or "")
        if leader_running
        else str(active_node_run.get("runtime_session_id") or "")
    )
    active_runtime_scope_key = (
        str(run_state.get("runtime_scope_key") or "")
        if leader_running
        else str(active_node_run.get("runtime_scope_key") or "")
    )
    run_started_at = (
        run_state.get("run_started_at") or 0
        if leader_running
        else active_node_run.get("started_at") or 0
    )
    run_updated_at = max(
        float(run_state.get("run_updated_at") or 0),
        float(active_node_run.get("updated_at") or 0),
    )
    projected_state = "waiting_approval" if waiting_approval else "running" if running else (
        "completed" if mission_status == "completed"
        else "failed" if mission_status == "failed"
        else "cancelled" if mission_status in {"cancelled", "canceled"}
        else "idle"
    )
    projection = {
        "running": running,
        "run_state": projected_state,
        "activity_state": projected_state,
        "waiting_approval": waiting_approval,
        "pending_approval_count": len(pending_approvals) if pending_approvals else (1 if waiting_approval else 0),
        "pending_approvals": pending_approvals,
        "mission_status": mission_status,
        "status": mission_status or str(conversation.get("status") or "").strip(),
        "active_mission_id": mission_id,
        "activeMissionId": mission_id,
        "active_run_id": active_run_id if running else "",
        "active_turn_id": active_turn_id if running else "",
        "active_runtime_session_id": active_runtime_session_id if running else "",
        "runtime_scope_key": active_runtime_scope_key if running else "",
        "run_started_at": run_started_at if running else 0,
        "run_updated_at": run_updated_at,
        "active_node_count": active_node_count,
    }
    if isinstance(runtime_summary, dict):
        projection.update({
            "task_frames": list(runtime_summary.get("task_frames") or []),
            "task_frame_count": int(runtime_summary.get("task_frame_count") or 0),
            "active_task_frame": runtime_summary.get("active_task_frame") if isinstance(runtime_summary.get("active_task_frame"), dict) else {},
            "run_session_ids": list(runtime_summary.get("run_session_ids") or []),
            "last_message": runtime_summary.get("last_message") if isinstance(runtime_summary.get("last_message"), dict) else {},
            "last_message_preview": str(runtime_summary.get("last_message_preview") or ""),
            "last_message_at": runtime_summary.get("last_message_at") or 0,
            "final_deliverables": list(runtime_summary.get("final_deliverables") or []),
            "artifact_refs": list(runtime_summary.get("artifact_refs") or []),
        })
    return projection


def _profile_params_from_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    profile_payload = dict(payload)
    profile = profile_payload.get("dovie_profile") or profile_payload.get("dovieProfile") or profile_payload.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    requested_scope_key = str(
        profile_payload.get("runtime_scope_key")
        or profile_payload.get("runtimeScopeKey")
        or ""
    ).strip()
    explicit_profile_scope_key = str(
        profile_payload.get("profile_runtime_scope_key")
        or profile_payload.get("profileRuntimeScopeKey")
        or profile.get("profileRuntimeScopeKey")
        or profile.get("profile_runtime_scope_key")
        or ""
    ).strip()
    if requested_scope_key.startswith(("team:", "team_mission:")):
        if explicit_profile_scope_key:
            profile_payload["runtime_scope_key"] = explicit_profile_scope_key
            profile_payload["runtimeScopeKey"] = explicit_profile_scope_key
        else:
            profile_payload.pop("runtime_scope_key", None)
            profile_payload.pop("runtimeScopeKey", None)
    context = _profile_context_for_params(profile_payload)
    if not isinstance(context, dict):
        return {}
    profile_id = str(context.get("id") or "").strip()
    version_id = str(context.get("agent_profile_version_id") or "").strip()
    draft_id = str(context.get("agent_profile_draft_id") or "").strip()
    hermes_home = str(context.get("hermes_home") or "").strip()
    runtime_scope_key = str(context.get("runtime_scope_key") or "").strip()
    result = {}
    if profile_id:
        result["agent_profile_id"] = profile_id
    if version_id:
        result["agent_profile_version_id"] = version_id
    if draft_id:
        result["agent_profile_draft_id"] = draft_id
    if runtime_scope_key:
        result["runtime_scope_key"] = runtime_scope_key
    if hermes_home:
        result["hermesHomePath"] = hermes_home
        result["dovie_profile"] = {
            "id": profile_id,
            "hermesHomePath": hermes_home,
            **({"agentProfileVersionId": version_id} if version_id else {}),
            **({"agentProfileDraftId": draft_id} if draft_id else {}),
            **({"runtimeScopeKey": runtime_scope_key} if runtime_scope_key else {}),
        }
    return result


def _profile_params_from_member(member: dict) -> dict:
    if not isinstance(member, dict):
        return {}
    profile = (
        member.get("dovie_profile")
        if isinstance(member.get("dovie_profile"), dict)
        else member.get("dovieProfile")
        if isinstance(member.get("dovieProfile"), dict)
        else {}
    )
    payload = {
        "agent_profile_id": str(
            member.get("profile_id")
            or member.get("profileId")
            or member.get("agent_profile_id")
            or member.get("agentProfileId")
            or profile.get("id")
            or profile.get("agentProfileId")
            or profile.get("agent_profile_id")
            or ""
        ).strip(),
        "agent_profile_version_id": str(
            member.get("profile_version_id")
            or member.get("profileVersionId")
            or member.get("agent_profile_version_id")
            or member.get("agentProfileVersionId")
            or member.get("version_id")
            or member.get("versionId")
            or profile.get("agentProfileVersionId")
            or profile.get("agent_profile_version_id")
            or profile.get("versionId")
            or profile.get("version_id")
            or ""
        ).strip(),
        "agent_profile_draft_id": str(
            member.get("profile_draft_id")
            or member.get("agent_profile_draft_id")
            or member.get("agentProfileDraftId")
            or profile.get("agentProfileDraftId")
            or profile.get("agent_profile_draft_id")
            or ""
        ).strip(),
        "runtime_scope_key": str(
            member.get("runtime_scope_key")
            or member.get("runtimeScopeKey")
            or profile.get("runtimeScopeKey")
            or profile.get("runtime_scope_key")
            or ""
        ).strip(),
    }
    hermes_home = str(
        member.get("hermes_home_path")
        or member.get("hermesHomePath")
        or profile.get("hermesHomePath")
        or profile.get("hermes_home_path")
        or profile.get("hermes_home")
        or ""
    ).strip()
    if hermes_home:
        payload["dovie_profile"] = {
            **profile,
            "id": payload["agent_profile_id"] or str(profile.get("id") or "").strip(),
            "hermesHomePath": hermes_home,
            **({"agentProfileVersionId": payload["agent_profile_version_id"]} if payload["agent_profile_version_id"] else {}),
            **({"agentProfileDraftId": payload["agent_profile_draft_id"]} if payload["agent_profile_draft_id"] else {}),
            **({"runtimeScopeKey": payload["runtime_scope_key"]} if payload["runtime_scope_key"] else {}),
        }
    return _profile_params_from_payload(payload)


def _member_matches_node_profile(member: dict, node: dict) -> bool:
    profile = (
        member.get("dovie_profile")
        if isinstance(member.get("dovie_profile"), dict)
        else member.get("dovieProfile")
        if isinstance(member.get("dovieProfile"), dict)
        else {}
    )
    member_profile_id = str(
        member.get("profile_id")
        or member.get("profileId")
        or member.get("agent_profile_id")
        or member.get("agentProfileId")
        or profile.get("id")
        or profile.get("agentProfileId")
        or profile.get("agent_profile_id")
        or ""
    ).strip()
    node_profile_id = str(node.get("assignee_profile_id") or "").strip()
    if node_profile_id and member_profile_id == node_profile_id:
        return True
    role = str(member.get("role") or "").strip()
    return _node_role(node) == "leader" and role in {"lead", "leader"}


def _node_profile_params(params: dict, mission: dict, node: dict, *, db=None) -> dict:
    explicit = _profile_params_from_payload(params)
    if explicit:
        return explicit
    has_team_identity = bool(
        str(params.get("team_id") or params.get("teamId") or "").strip()
        or (isinstance(mission, dict) and str(mission.get("team_id") or mission.get("teamId") or "").strip())
    )
    has_node_assignee = bool(str(node.get("assignee_profile_id") or node.get("assigneeProfileId") or "").strip())
    members = _leader_members_from_params(
        params,
        mission if isinstance(mission, dict) else {},
        db=db,
        strict=has_team_identity and has_node_assignee,
    )
    for member in members:
        if _member_matches_node_profile(member, node):
            profile_params = _profile_params_from_member(member)
            if profile_params:
                return profile_params
    return _profile_params_from_payload({
        "agent_profile_id": str(node.get("assignee_profile_id") or "").strip(),
        "agent_profile_version_id": str(node.get("assignee_profile_version_id") or "").strip(),
        "runtime_scope_key": str(node.get("runtime_scope_key") or "").strip(),
    })


def _leader_profile_params(params: dict, graph: dict) -> dict:
    explicit = _profile_params_from_payload(params)
    if explicit:
        return explicit
    root = _root_leader_node(graph)
    mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
    mission_id = str(mission.get("mission_id") or "").strip()
    conversation_id = _conversation_id_from_params(params, {}) or _conversation_session_id_from_params(params, {})
    scope_subject = (
        conversation_id
        or str(mission.get("conversation_id") or "").strip()
        or mission_id
    )
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    members = metadata.get("members") if isinstance(metadata.get("members"), list) else []
    leader = next(
        (
            item for item in members
            if isinstance(item, dict) and str(item.get("role") or "").strip() in {"lead", "leader"}
        ),
        None,
    ) or next((item for item in members if isinstance(item, dict)), {})
    if leader:
        profile_params = _profile_params_from_member(leader)
        if not profile_params.get("runtime_scope_key"):
            profile_params["runtime_scope_key"] = str(f"team:{scope_subject}:leader-conversation").strip()
        return profile_params
    return {
        "agent_profile_id": str(root.get("assignee_profile_id") or "").strip(),
        "agent_profile_version_id": str(root.get("assignee_profile_version_id") or "").strip(),
        "runtime_scope_key": str(root.get("runtime_scope_key") or f"team:{scope_subject}:leader-conversation").strip(),
    }


def _resolve_team_leader_runtime_params_for_request(params: dict, graph: dict, db) -> tuple[dict, dict]:
    try:
        resolution = resolve_team_leader_runtime_params(params, db=db)
        return resolution.params, resolution.leader_runtime_context
    except ValueError:
        profile_params = _leader_profile_params(params, graph if isinstance(graph, dict) else {})
        if any(str(profile_params.get(key) or "").strip() for key in (
            "agent_profile_id",
            "agent_profile_version_id",
            "agent_profile_draft_id",
            "runtime_scope_key",
            "hermesHomePath",
        )):
            return params, {}
        raise


def _profile_runtime_owned(profile_params: dict) -> bool:
    if not isinstance(profile_params, dict):
        return False
    scope_key = str(profile_params.get("runtime_scope_key") or "").strip()
    return bool(
        scope_key.startswith(("profile:", "draft:"))
        or profile_params.get("agent_profile_draft_id")
        or profile_params.get("dovie_profile")
        or profile_params.get("hermesHomePath")
    )


def _leader_conversation_runtime_scope_key(params: dict, *, conversation_id: str = "", mission_id: str = "") -> str:
    requested = str(params.get("runtime_scope_key") or params.get("runtimeScopeKey") or "").strip()
    if requested.startswith("team:"):
        return requested
    return str(
        params.get("leader_runtime_scope_key")
        or params.get("leaderRuntimeScopeKey")
        or params.get("leaderRuntimeScopeKey")
        or params.get("conversation_runtime_scope_key")
        or params.get("conversationRuntimeScopeKey")
        or params.get("team_leader_runtime_scope_key")
        or params.get("teamLeaderRuntimeScopeKey")
        or f"team:{conversation_id or mission_id}:leader-conversation"
    ).strip()


def _leader_conversation_runtime_scope_contract_error(params: dict, expected_scope_key: str) -> str:
    requested = str(params.get("runtime_scope_key") or params.get("runtimeScopeKey") or "").strip()
    if not requested:
        return ""
    if requested == expected_scope_key:
        return ""
    if requested.startswith("team:"):
        return (
            "team leader conversation runtime scope mismatch; "
            f"expected {expected_scope_key}, received {requested}"
        )
    return (
        "team leader conversation must use the team conversation runtime scope as runtimeScopeKey; "
        f"received {requested}, expected {expected_scope_key}. "
        "Pass the leader profile scope as profileRuntimeScopeKey or dovie_profile.runtimeScopeKey instead."
    )


def _leader_runtime_owner_error(profile_params: dict, *, leader_runtime_scope_key: str = "") -> str:
    if not _profile_runtime_owned(profile_params):
        return ""
    expected = str(profile_params.get("runtime_scope_key") or "").strip()
    leader_expected = str(leader_runtime_scope_key or "").strip()
    if not expected and not leader_expected:
        return ""
    current = str(os.environ.get("DOVIE_HERMES_RUNTIME_SCOPE_KEY") or "").strip()
    # The canonical conversation RPC runs on the control plane; only reject a
    # request that is already executing inside a conflicting scoped worker.
    if not current:
        return ""
    if current in {expected, leader_expected}:
        return ""
    expected_desc = leader_expected or expected
    return (
        "team leader conversation must run inside its owner runtime scope "
        f"{expected_desc}; current scope is {current or 'control-plane'}"
    )


def _compact_graph_context(graph: dict) -> dict:
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    conversation = graph.get("conversation") if isinstance(graph, dict) and isinstance(graph.get("conversation"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    edges = graph.get("edges") if isinstance(graph, dict) else []
    compact_nodes = []
    for node in nodes[:24] if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        compact_nodes.append({
            "id": str(node.get("node_id") or ""),
            "kind": str(node.get("kind") or ""),
            "title": str(node.get("title") or "")[:120],
            "status": str(node.get("status") or ""),
            "role": str(metadata.get("role") or ""),
            "phase": str(metadata.get("phase") or ""),
        })
    return {
        "conversation": {
            "id": str((conversation or {}).get("conversation_id") or ""),
            "title": str((conversation or {}).get("title") or "")[:160],
            "stable_session_id": str((conversation or {}).get("stable_session_id") or ""),
            "active_mission_id": str((conversation or {}).get("active_mission_id") or ""),
        },
        "mission": {
            "id": str((mission or {}).get("mission_id") or ""),
            "title": str((mission or {}).get("title") or "")[:160],
            "objective": str((mission or {}).get("objective") or "")[:500],
            "mode": str((mission or {}).get("mode") or ""),
            "status": str((mission or {}).get("status") or ""),
        },
        "node_count": len(nodes) if isinstance(nodes, list) else 0,
        "edge_count": len(edges) if isinstance(edges, list) else 0,
        "nodes": compact_nodes,
    }


def _team_memory_for_leader_message(db, params: dict, mission: dict, *, objective: str) -> tuple[dict, str]:
    if _team_memory_disabled(params, mission):
        return {"disabled": True, "reason": "disabled_by_request_or_policy"}, ""
    mission_id = str(mission.get("mission_id") or "").strip()
    if not mission_id:
        return {}, ""
    try:
        payload = db.build_team_mission_memory_pack(
            mission_id=mission_id,
            objective=objective,
            workspace_id=str(mission.get("workspace_id") or ""),
            limit=int(params.get("memory_limit") or params.get("memoryLimit") or 8),
            include_team_scope=_team_memory_include_team_scope(params, mission),
        )
        text = _memory_context_text(label="Team Conversation Memory Pack", payload=payload)
        memory = payload.get("memory_pack") if isinstance(payload, dict) else {}
        return {
            "kind": "leader_conversation_memory_pack",
            "conversation_session_id": str(payload.get("conversation_session_id") or "") if isinstance(payload, dict) else "",
            "item_ids": list((memory or {}).get("item_ids") or []),
            "artifact_refs": list((memory or {}).get("artifact_refs") or []),
        }, text
    except Exception as exc:
        return {"disabled": True, "reason": f"memory_build_failed: {exc}"}, ""


def _record_leader_input_attachment_artifacts(
    db,
    *,
    mission: dict,
    conversation_session_id: str,
    run_id: str,
    attachments: list[dict],
) -> list[dict]:
    artifact_refs = artifact_refs_from_payload({"attachments": attachments}, event_type="team_mission.message.submit")
    if not artifact_refs:
        return []
    mission_id = str((mission or {}).get("mission_id") or "").strip()
    if not mission_id:
        return []
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    task_id = _task_id_from_metadata(metadata) or mission_id
    titles = [
        str(ref.get("title") or ref.get("path") or ref.get("uri") or ref.get("id") or "").strip()
        for ref in artifact_refs
    ]
    content = "User submitted attachments: " + ", ".join(title for title in titles if title)
    try:
        item = db.upsert_team_mission_memory_item(
            memory_id=f"team-input-artifacts:{mission_id}:{task_id}:{run_id}",
            team_id=str((mission or {}).get("team_id") or mission_id),
            mission_id=mission_id,
            conversation_session_id=str(conversation_session_id or ""),
            task_id=task_id,
            scope="mission_task",
            kind="artifact",
            content=content,
            structured_payload={
                "source": "team_mission.message.submit",
                "attachments": attachments,
            },
            source_node_ids=[],
            source_run_ids=[str(run_id or "")],
            artifact_refs=artifact_refs,
            workspace_refs=[{
                "workspace_id": str((mission or {}).get("workspace_id") or ""),
                "workspace_path": str((mission or {}).get("workspace_path") or ""),
            }] if ((mission or {}).get("workspace_id") or (mission or {}).get("workspace_path")) else [],
            confidence=0.98,
            visibility="team",
            status="committed",
        )
    except Exception:
        return []
    return [item] if isinstance(item, dict) and item else []


def _leader_router_prompt(*, user_text: str, graph: dict, memory_text: str = "") -> str:
    context_json = json.dumps(_compact_graph_context(graph), ensure_ascii=False, indent=2)
    parts = [
        "You are the Team Leader for a DoXie team conversation.",
        "Keep the same persona, identity, tone, and memory as the underlying DoXie profile. Team mode only adds team context and team coordination tools.",
        "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is DoXie.",
        "",
        "Route this user message before acting:",
        "- Answer directly for greetings, status questions, explanations, follow-up questions, clarifications, or requests about prior/current work.",
        "- Use clarify, file, terminal, or todo tools when they help you understand the user's request, inspect the workspace, validate local context, or organize the plan before deciding whether to start a team task.",
        "- Use team_mission_status when you need fresh mission graph or memory context to answer.",
        "- Call team_mission_start_task only when the user is asking to start a new substantive executable team task that benefits from planning, multi-agent work, workspace changes, research, verification, or a deliverable.",
        "- Do not call team_mission_start_task for greetings, lightweight Q&A, status checks, or discussion that can be answered directly.",
        "- Do not call delegate_task or ordinary subagents. In DoXie team mode, the Leader coordinates the user conversation, task graph, and member nodes.",
        "- If you start a task, keep your visible reply brief and tell the user that planning has started.",
        "- After team_mission_start_task succeeds, stop the current turn. Do not continue with research, file work, terminal commands, or deliverable execution.",
        "- Reply in the user's language.",
        "",
        "Current team conversation context. The active mission may be empty until a team task is started:",
        context_json,
        "",
        "A team task is created only when you call team_mission_start_task. "
        "For a new task, derive the mission title and objective from the current User message, not from the conversation title.",
    ]
    if memory_text:
        parts.extend(["", memory_text])
    parts.extend(["", "User message:", user_text])
    return "\n".join(parts).strip()


def _leader_direct_reply_prompt(*, user_text: str, graph: dict, memory_text: str = "") -> str:
    parts = [
        "You are the Team Leader in a DoXie team conversation.",
        "Keep the same persona, identity, tone, and memory as the underlying DoXie profile. Team mode only adds team context and team coordination tools.",
        "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is DoXie.",
        "The user explicitly asked you not to start or launch a team task for this turn.",
        "Answer directly in the user's language. Do not call tools, do not create tasks, and do not mention internal routing.",
    ]
    context = _compact_graph_context(graph)
    active_mission = context.get("mission") if isinstance(context, dict) else {}
    if isinstance(active_mission, dict) and active_mission:
        parts.extend(
            [
                "",
                "Current team conversation context for reference only:",
                json.dumps(context, ensure_ascii=False, indent=2),
            ]
        )
    if memory_text:
        parts.extend(["", memory_text])
    parts.extend(["", "User message:", user_text])
    return "\n".join(parts).strip()


def _normalized_marker_text(user_text: str) -> tuple[str, str]:
    normalized = " ".join(str(user_text or "").strip().lower().split())
    return normalized, normalized.replace(" ", "")


def _has_normalized_marker(normalized: str, compact: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        candidate = str(marker or "").strip().lower()
        if not candidate:
            continue
        if candidate in normalized or candidate.replace(" ", "") in compact:
            return True
    return False


def _leader_message_requests_team_task_start(user_text: str) -> bool:
    normalized, compact = _normalized_marker_text(user_text)
    if not normalized:
        return False
    if _has_normalized_marker(normalized, compact, _TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS):
        return False
    return _has_normalized_marker(normalized, compact, _TEAM_LEADER_START_TASK_MARKERS)


def _leader_message_requests_direct_reply(user_text: str) -> bool:
    normalized, compact = _normalized_marker_text(user_text)
    if not normalized:
        return False
    if _has_normalized_marker(normalized, compact, _TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS):
        return True
    if _has_normalized_marker(normalized, compact, _TEAM_LEADER_DIRECT_REPLY_NEGATED_SELF_MARKERS):
        return False
    if _leader_message_requests_team_task_start(user_text):
        return False
    return _has_normalized_marker(normalized, compact, _TEAM_LEADER_DIRECT_REPLY_SELF_MARKERS)


def _leader_message_toolsets(params: dict) -> list[str]:
    return _merge_toolsets(
        _TEAM_LEADER_CONVERSATION_TOOLSETS,
        params.get("enabled_toolsets") or params.get("enabledToolsets"),
    )


def _ensure_team_conversation_session(db, conversation_session_id: str) -> bool:
    conversation_session_id = str(conversation_session_id or "").strip()
    if not conversation_session_id:
        raise ValueError("conversation_session_id required")
    if db.get_session(conversation_session_id):
        return False
    ensure_session = getattr(db, "ensure_session", None)
    if callable(ensure_session):
        ensure_session(conversation_session_id, source="team_mission", transient=False)
    else:
        db.create_session(conversation_session_id, source="team_mission", transient=False)
    return True


def _schedule_ready_nodes(
    *,
    db,
    rid,
    params: dict,
    trigger: str = "team_mission.schedule.ready",
) -> dict:
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return {}
    scheduler = TeamMissionReadyScheduler(
        db=db,
        start_node=lambda start_rid, start_params: _methods["team_mission.node.start"](start_rid, start_params),
    )
    return scheduler.schedule_ready_nodes(
        mission_id=mission_id,
        rid=rid,
        params=params,
        limit=_bounded_limit(params.get("limit"), default=10, maximum=50),
        dry_run=bool(params.get("dry_run") or params.get("dryRun")),
        trigger=trigger,
    )


def _schedule_ready_nodes_from_runtime_event(
    *,
    mission_id: str,
    db=None,
    trigger_event: str = "",
    run_id: str = "",
) -> dict:
    active_db = db or _get_db()
    if active_db is None:
        return {}
    return _schedule_ready_nodes(
        db=active_db,
        rid=None,
        params={"mission_id": mission_id},
        trigger=f"run_event:{trigger_event or 'terminal'}:{run_id or ''}",
    )


run_control.register_team_mission_ready_scheduler(_schedule_ready_nodes_from_runtime_event)


@method("team_capability.snapshot.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    snapshot_id = _team_capability_snapshot_id(params) or str(params.get("snapshot_id") or params.get("snapshotId") or "").strip()
    if snapshot_id:
        snapshot = db.get_team_capability_snapshot(snapshot_id)
        if not snapshot:
            return _err(rid, 4040, "team capability snapshot not found")
        return _ok(rid, {"snapshot": snapshot})
    team_id = str(params.get("team_id") or params.get("teamId") or "").strip()
    if not team_id:
        return _err(rid, 4006, "team_id or snapshot_id required")
    try:
        snapshot = _resolve_team_capability_snapshot_from_registry(db, params, team_id=team_id)
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot unavailable: {exc}")
    return _ok(rid, {"snapshot": snapshot})


@method("team_capability.snapshot.refresh")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    team_id = str(params.get("team_id") or params.get("teamId") or "").strip()
    if not team_id:
        return _err(rid, 4006, "team_id required")
    try:
        snapshot = _resolve_team_capability_snapshot_from_registry(db, params, team_id=team_id, force_refresh=True)
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot refresh failed: {exc}")
    return _ok(rid, {"snapshot": snapshot})


@method("team_capability.snapshot.bind")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        snapshot = _resolve_team_capability_snapshot_for_params(
            db,
            params,
            team_id=str(params.get("team_id") or params.get("teamId") or ""),
        )
        if not snapshot:
            return _err(rid, 4006, "snapshot_id or team_id required")
        binding = _bind_team_capability_snapshot_for_mission(
            db,
            mission_id=mission_id,
            conversation_id=_conversation_id_from_params(params, {}),
            snapshot=snapshot,
        )
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot bind failed: {exc}")
    return _ok(rid, {"snapshot": snapshot, "binding": binding})


@method("team_mission.team_profile.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    snapshot_id = _team_capability_snapshot_id(params) or str(params.get("snapshot_id") or params.get("snapshotId") or "").strip()
    mission = {}
    conversation = {}
    if mission_id:
        graph = db.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) and isinstance(graph.get("mission"), dict) else {}
    if not mission_id:
        identifier = (
            _conversation_id_from_params(params, {})
            or _conversation_session_id_from_params(params, {})
            or str(params.get("identifier") or params.get("id") or "").strip()
        )
        resolved = db.resolve_team_mission_conversation(identifier) if identifier else {}
        conversation = resolved.get("conversation") if isinstance(resolved, dict) and isinstance(resolved.get("conversation"), dict) else {}
        mission = resolved.get("mission") if isinstance(resolved, dict) and isinstance(resolved.get("mission"), dict) else {}
        mission_id = str(mission.get("mission_id") or "").strip()
    try:
        team_id = _team_id_for_profile(params, mission=mission, conversation=conversation)
        snapshot = db.get_team_capability_snapshot(snapshot_id) if snapshot_id else {}
        binding = {}
        source = "snapshot_id" if snapshot else ""
        if mission_id:
            binding = db.get_team_capability_snapshot_binding(mission_id)
            if not snapshot:
                snapshot = db.get_bound_team_capability_snapshot(mission_id)
                if snapshot:
                    source = "mission_binding"
        if not snapshot:
            snapshot = db.get_latest_team_capability_snapshot(team_id) if team_id else {}
            if snapshot:
                source = "latest_team_snapshot"
        if not snapshot and team_id:
            snapshot = _resolve_team_capability_snapshot_from_registry(db, params, team_id=team_id)
            if snapshot:
                source = "team_registry"
    except Exception as exc:
        return _err(rid, 5008, f"team profile unavailable: {exc}")
    if not snapshot:
        return _err(rid, 4040, "team capability snapshot not found")
    return _ok(rid, {"mission_id": mission_id, "team_id": team_id, "binding": binding, "snapshot": snapshot, "source": source})


@method("team_mission.create")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    team_id = str(params.get("team_id") or params.get("teamId") or "").strip()
    archived_team_error = _archived_team_write_error(db, team_id)
    if archived_team_error:
        return _err(rid, 4023, archived_team_error)
    try:
        mode = _resolve_team_mission_create_mode(db, params, team_id=team_id)
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    members = []
    graph_payload = (
        params.get("graph_payload")
        or params.get("graphPayload")
        or {}
    )
    if not isinstance(graph_payload, dict):
        return _err(rid, 4004, "graph_payload must be an object")
    metadata = params.get("metadata") or {}
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "metadata must be an object")
    metadata = _normalize_mission_metadata(params, metadata)
    conversation_id = _conversation_id_from_params(params, metadata) or mission_id
    leader_session_id = str(params.get("leader_session_id") or params.get("leaderSessionId") or "").strip()
    if not leader_session_id:
        leader_session_id = _conversation_session_id_from_params(params, metadata)
    if _conversation_only_from_params(params):
        metadata = {
            **metadata,
            "conversation_only": True,
            "start_leader": False,
        }
        if not conversation_id:
            return _err(rid, 4006, "conversation_id required")
        conversation_session_id = _conversation_session_id_from_params(params, metadata) or conversation_id
        try:
            workspace_context = resolve_team_mission_workspace_context(
                params,
                session_id=conversation_session_id,
                require=True,
            )
        except ValueError as exc:
            return _err(rid, 4004, str(exc))
        try:
            conversation = db.ensure_team_mission_conversation(
                conversation_id=conversation_id,
                stable_session_id=conversation_session_id,
                team_id=team_id,
                title=str(params.get("title") or ""),
                objective=str(params.get("conversation_objective") or params.get("conversationObjective") or ""),
                workspace_id=workspace_context["workspace_id"],
                workspace_path=workspace_context["workspace_path"],
                created_by_user_id=str(params.get("created_by_user_id") or params.get("createdByUserId") or ""),
                metadata=metadata,
            )
            bind_team_mission_session_workspace(
                session_id=conversation_session_id,
                context=workspace_context,
                metadata={
                    "source": "team_mission.create",
                    "conversation_id": conversation_id,
                    "team_id": team_id,
                    "conversation_only": True,
                },
            )
        except ValueError as exc:
            return _err(rid, 4004, str(exc))
        except Exception as exc:
            return _err(rid, 5008, f"team mission conversation create failed: {exc}")
        return _ok(rid, {
            "mission_id": "",
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "conversation": conversation,
            "graph": {
                "mission": {},
                "conversation": conversation,
                "nodes": [],
                "edges": [],
                "run_bindings": [],
            },
        })
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if team_id:
        try:
            members = _team_runtime_members_from_registry(db, params, team_id=team_id)
        except ValueError as exc:
            return _err(rid, 4006, str(exc))
        except Exception as exc:
            return _err(rid, 5008, f"team members unavailable: {exc}")
    else:
        return _err(rid, 4006, "team_id required")
    capability_snapshot = {}
    try:
        capability_snapshot = _resolve_team_capability_snapshot_for_params(
            db,
            params,
            team_id=team_id,
        )
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot unavailable: {exc}")
    if capability_snapshot:
        metadata["team_capability_snapshot"] = _snapshot_binding_metadata(capability_snapshot)
        members = _members_with_capability_snapshot(members, capability_snapshot)
    conversation_session_id = leader_session_id or _conversation_session_id_from_params(params, metadata) or conversation_id
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            session_id=conversation_session_id,
            require=True,
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    try:
        graph = db.initialize_team_mission_from_strategy(
            mission_id=mission_id,
            conversation_id=conversation_id,
            team_id=team_id,
            title=str(params.get("title") or ""),
            objective=str(params.get("objective") or params.get("prompt") or ""),
            workspace_id=workspace_context["workspace_id"],
            workspace_path=workspace_context["workspace_path"],
            mode=mode,
            members=members,
            graph_payload=graph_payload,
            leader_session_id=leader_session_id,
            metadata=metadata,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.create",
                "conversation_id": conversation_id,
                "mission_id": mission_id,
                "team_id": team_id,
            },
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team mission create failed: {exc}")
    if capability_snapshot:
        try:
            _bind_team_capability_snapshot_for_mission(
                db,
                mission_id=mission_id,
                conversation_id=conversation_id,
                snapshot=capability_snapshot,
            )
            graph = db.get_team_mission_graph(mission_id)
        except Exception as exc:
            return _err(rid, 5008, f"team capability snapshot bind failed: {exc}")
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    root_node = next(
        (
            node for node in graph.get("nodes", [])
            if isinstance(node, dict) and str(node.get("kind") or "") == "root"
        ),
        None,
    )
    task_id = str(params.get("task_id") or params.get("taskId") or "").strip()
    if root_node and task_id:
        root_metadata = root_node.get("metadata") if isinstance(root_node.get("metadata"), dict) else {}
        root_metadata = {
            **root_metadata,
            "submitted_task_id": task_id,
            "task_id": task_id,
            "task_title": str(params.get("title") or root_node.get("title") or ""),
            "task_objective": str(params.get("objective") or params.get("prompt") or root_node.get("objective") or ""),
        }
        root_node = db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id=str(root_node.get("node_id") or ""),
            kind=str(root_node.get("kind") or "root"),
            title=str(root_node.get("title") or ""),
            objective=str(root_node.get("objective") or ""),
            status=str(root_node.get("status") or "running"),
            assignee_profile_id=str(root_node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(root_node.get("assignee_profile_version_id") or ""),
            runtime_scope_key=str(root_node.get("runtime_scope_key") or ""),
            output_contract=root_node.get("output_contract") if isinstance(root_node.get("output_contract"), dict) else {},
            metadata=root_metadata,
            position_x=float(root_node.get("position_x") or 0),
            position_y=float(root_node.get("position_y") or 0),
        )
        graph = db.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else {}
    if (
        isinstance(mission, dict)
        and mission
        and not _falsey(params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"))
    ):
        _append_team_user_task_message(
            db,
            mission=mission,
            objective=str(params.get("objective") or params.get("prompt") or ""),
            node_id=str((root_node or {}).get("node_id") or ""),
            task_id=task_id or mission_id,
        )
    start_response = None
    if root_node and metadata.get("start_leader") is True:
        start_response = _methods["team_mission.node.start"](
            rid,
            {
                "mission_id": mission_id,
                "node_id": str(root_node.get("node_id") or ""),
                "use_strategy_prompt": True,
                "record_user_task_message": params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"),
                "members": members,
            },
        )
        if isinstance(start_response, dict) and start_response.get("error"):
            return start_response
        graph = db.get_team_mission_graph(mission_id)
    result = {"mission_id": mission_id, "conversation_id": conversation_id, "graph": graph}
    if isinstance(start_response, dict):
        result["leader_start"] = start_response.get("result") or {}
    return _ok(rid, result)


@method("team_mission.conversation.ensure")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    graph = db.get_team_mission_graph(mission_id) if mission_id else {}
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    conversation_id = (
        _conversation_id_from_params(params, metadata)
        or (str(mission.get("conversation_id") or "").strip() if isinstance(mission, dict) else "")
        or mission_id
    )
    conversation_session_id = _conversation_session_id_from_params(params, metadata)
    if not conversation_session_id and isinstance(mission, dict) and mission:
        conversation_session_id = _team_conversation_session_id(mission)
    if not conversation_id and conversation_session_id:
        conversation_id = conversation_session_id
    if not conversation_id:
        return _err(rid, 4006, "conversation_id required")
    params = {
        **params,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        **({"conversation_session_id": conversation_session_id, "conversationSessionId": conversation_session_id} if conversation_session_id else {}),
        **({"mission_id": mission_id, "missionId": mission_id} if mission_id else {}),
    }
    before = db.get_team_mission_conversation(conversation_id)
    archived_team_error = _archived_team_write_error(
        db,
        _team_id_for_profile(params, mission=mission if isinstance(mission, dict) else {}, conversation=before),
    )
    if archived_team_error:
        return _err(rid, 4023, archived_team_error)
    try:
        params, leader_runtime_context = _resolve_team_leader_runtime_params_for_request(
            params,
            graph if isinstance(graph, dict) else {},
            db,
        )
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    profile_params = _leader_profile_params(params, graph if isinstance(graph, dict) else {})
    runtime_scope_key = _leader_conversation_runtime_scope_key(
        params,
        conversation_id=conversation_id,
        mission_id=mission_id,
    )
    contract_error = _leader_conversation_runtime_scope_contract_error(params, runtime_scope_key)
    if contract_error:
        return _err(rid, 4094, contract_error)
    owner_error = _leader_runtime_owner_error(profile_params, leader_runtime_scope_key=runtime_scope_key)
    if owner_error:
        return _err(rid, 4094, owner_error)
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=mission if isinstance(mission, dict) else {},
            conversation=before if isinstance(before, dict) else {},
            session_id=conversation_session_id,
            require=True,
        )
        bound_mission_id = mission_id if isinstance(mission, dict) and mission else ""
        conversation = db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            stable_session_id=conversation_session_id,
            mission=mission if isinstance(mission, dict) else {},
            mission_id=bound_mission_id,
            team_id=str(params.get("team_id") or params.get("teamId") or ""),
            title="",
            objective=str(params.get("objective") or params.get("prompt") or ""),
            workspace_id=workspace_context["workspace_id"],
            workspace_path=workspace_context["workspace_path"],
            created_by_user_id=str(params.get("created_by_user_id") or params.get("createdByUserId") or ""),
            metadata=metadata,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.conversation.ensure",
                "conversation_id": conversation_id,
                **({"mission_id": mission_id} if mission_id else {}),
                "team_id": str(params.get("team_id") or params.get("teamId") or ""),
            },
        )
        created = not bool(before)
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team mission conversation unavailable: {exc}")
    conversation_session_id = str(
        (conversation or {}).get("stable_session_id")
        or conversation_session_id
        or ""
    )
    graph = db.get_team_mission_graph(mission_id) if mission_id else {}
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "created": created,
            "conversation": conversation,
            "session": db.get_session(conversation_session_id) or {},
            "leader_runtime_context": leader_runtime_context,
            "leaderRuntimeContext": leader_runtime_context,
            "graph": graph if isinstance(graph, dict) else {},
        },
    )


@method("team_mission.conversation.resolve")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    identifier = str(
        params.get("identifier")
        or params.get("id")
        or _conversation_id_from_params(params, metadata)
        or _mission_id_from_params(params)
        or _conversation_session_id_from_params(params, metadata)
        or ""
    ).strip()
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    result = db.resolve_team_mission_conversation(identifier)
    if not result:
        return _ok(rid, {"conversation": {}, "mission": {}, "graph": {}})
    conversation = result.get("conversation") if isinstance(result, dict) else None
    _recover_conversation_active_run(db, conversation)
    if isinstance(conversation, dict):
        refreshed_identifier = str(
            conversation.get("conversation_id")
            or conversation.get("stable_session_id")
            or identifier
        ).strip()
        refreshed = db.resolve_team_mission_conversation(refreshed_identifier) if refreshed_identifier else {}
        if isinstance(refreshed, dict) and refreshed:
            result = refreshed
            conversation = result.get("conversation") if isinstance(result.get("conversation"), dict) else conversation
    if isinstance(conversation, dict):
        conversation.update(_conversation_runtime_projection(db, conversation))
    return _ok(rid, _attach_team_detail_projection(db, result))


@method("team_mission.conversation.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"conversations": []})
    conversations = db.list_team_mission_conversations(
        team_id=str(params.get("team_id") or params.get("teamId") or ""),
        workspace_id=_workspace_id_from_params(params),
        status=str(params.get("status") or ""),
        limit=_bounded_limit(params.get("limit"), default=100, maximum=500),
    )
    # 列表级查询(侧栏)只需要列表字段;运行态由 session_index 预计算提供。lightweight
    # 模式跳过对每个会话的画布级富化(recover active run + runtime projection:每会话
    # 查 missions/nodes/run_bindings/消息 + 逐个查 run)。那是 O(N) 富化,会话越多越慢,
    # 而画布详情本就应在打开会话时才加载,不属于列表职责。其他客户端(不传 lightweight)
    # 保持完整富化,行为不变。
    lightweight = bool(params.get("lightweight") or params.get("lite"))
    if not lightweight:
        for conversation in conversations if isinstance(conversations, list) else []:
            _recover_conversation_active_run(db, conversation)
            conversation.update(_conversation_runtime_projection(db, conversation))
    return _ok(rid, {"conversations": conversations})


@method("team_mission.conversation.runtime_session_ids")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"session_ids": [], "runtime_session_ids": []})
    getter = getattr(db, "list_team_mission_conversation_runtime_session_ids", None)
    if not callable(getter):
        return _ok(rid, {"session_ids": [], "runtime_session_ids": []})
    session_ids = getter(
        team_id=str(params.get("team_id") or params.get("teamId") or ""),
        workspace_id=_workspace_id_from_params(params),
        status=str(params.get("status") or ""),
        mission_id=str(params.get("mission_id") or params.get("missionId") or ""),
        limit=_bounded_limit(params.get("limit"), default=500, maximum=500),
    )
    normalized = []
    seen = set()
    for session_id in session_ids if isinstance(session_ids, list) else []:
        value = str(session_id or "").strip()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return _ok(rid, {
        "session_ids": normalized,
        "sessionIds": normalized,
        "runtime_session_ids": normalized,
        "runtimeSessionIds": normalized,
    })


@method("team_mission.conversation.rename")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    identifier = str(
        params.get("identifier")
        or params.get("id")
        or _conversation_id_from_params(params, metadata)
        or _mission_id_from_params(params)
        or _conversation_session_id_from_params(params, metadata)
        or ""
    ).strip()
    title = str(params.get("title") or "").strip()
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    if not title:
        return _err(rid, 4021, "title required")
    try:
        result = db.rename_team_mission_conversation(identifier, title)
    except ValueError as exc:
        return _err(rid, 4021, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team mission conversation rename failed: {exc}")
    if not result:
        return _err(rid, 4040, "team mission conversation not found")
    return _ok(rid, result)


@method("team_mission.conversation.delete")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    identifier = str(
        params.get("identifier")
        or params.get("id")
        or _conversation_id_from_params(params, metadata)
        or _mission_id_from_params(params)
        or _conversation_session_id_from_params(params, metadata)
        or ""
    ).strip()
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    resolved = db.resolve_team_mission_conversation(identifier)
    conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
    if not conversation:
        return _err(rid, 4040, "team mission conversation not found")
    stable_session_id = str(conversation.get("stable_session_id") or "").strip()
    if stable_session_id:
        run_state = run_control.session_status(
            stable_session_id,
            db=db,
            current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
        )
        if run_state.get("running"):
            return _err(rid, 4023, "cannot delete a conversation with an active leader run")
    try:
        result = db.delete_team_mission_conversation(identifier)
        deleted_session_ids = list((result or {}).get("deleted_session_ids") or [])
        artifact_cleanup = {
            "deleted_artifact_links": 0,
            "deleted_artifacts": 0,
            "deleted_artifact_ids": [],
            "physical_files_deleted": 0,
        }
        workspace_bindings = []
        workspace_binding_cleanup_error = ""
        if deleted_session_ids:
            try:
                artifact_cleanup = delete_session_artifacts(deleted_session_ids)
            except Exception as exc:
                artifact_cleanup = {
                    **artifact_cleanup,
                    "error": str(exc),
                }
            try:
                workspace_bindings = delete_session_workspace_bindings(deleted_session_ids)
            except Exception as exc:
                workspace_bindings = []
                workspace_binding_cleanup_error = str(exc)
        remove_session_files = getattr(db, "_remove_session_files", None)
        if callable(remove_session_files):
            sessions_dir = Path(get_hermes_home()) / "sessions"
            for session_id in dict.fromkeys(str(item or "").strip() for item in deleted_session_ids):
                if not session_id:
                    continue
                try:
                    remove_session_files(sessions_dir, session_id)
                except Exception:
                    pass
        if isinstance(result, dict):
            result["deleted_session_ids"] = deleted_session_ids
            result["artifact_cleanup"] = artifact_cleanup
            result["deleted_artifact_links"] = int(artifact_cleanup.get("deleted_artifact_links") or 0)
            result["deleted_artifacts"] = int(artifact_cleanup.get("deleted_artifacts") or 0)
            result["physical_files_deleted"] = int(artifact_cleanup.get("physical_files_deleted") or 0)
            result["deleted_workspace_bindings"] = workspace_bindings
            result["deleted_workspace_binding_count"] = len(workspace_bindings)
            if workspace_binding_cleanup_error:
                result["workspace_binding_cleanup_error"] = workspace_binding_cleanup_error
        if stable_session_id:
            result.setdefault("stable_session_id", stable_session_id)
    except Exception as exc:
        return _err(rid, 5008, f"team mission conversation delete failed: {exc}")
    if not result:
        return _err(rid, 4040, "team mission conversation not found")
    return _ok(rid, result)


@method("team_mission.message.submit")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    explicit_mission_request = bool(mission_id)
    text = _message_text_from_params(params)
    if not text:
        return _err(rid, 4006, "text required")
    conversation_id = _conversation_id_from_params(params, {})
    conversation_session_id = _conversation_session_id_from_params(params, {})
    if not mission_id and not conversation_id:
        return _err(rid, 4006, "mission_id or conversation_id required")
    graph = db.get_team_mission_graph(mission_id) if mission_id else {}
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if mission_id and (not isinstance(mission, dict) or not mission):
        if not conversation_id:
            return _err(rid, 4040, "team mission not found")
        mission_id = ""
        explicit_mission_request = False
        graph = {}
        mission = {}
    conversation = {}
    context_graph = graph if isinstance(graph, dict) else {}
    context_mission = mission if isinstance(mission, dict) else {}
    if not mission:
        resolved_identifier = conversation_id
        resolved = db.resolve_team_mission_conversation(resolved_identifier) if resolved_identifier else {}
        if isinstance(resolved, dict):
            conversation = resolved.get("conversation") if isinstance(resolved.get("conversation"), dict) else {}
            resolved_graph = resolved.get("graph") if isinstance(resolved.get("graph"), dict) else {}
            resolved_mission = resolved.get("mission") if isinstance(resolved.get("mission"), dict) else {}
            if resolved_mission:
                context_mission = resolved_mission
                context_graph = resolved_graph
                if explicit_mission_request:
                    mission = resolved_mission
                    graph = resolved_graph
                    mission_id = str(mission.get("mission_id") or "").strip()
    identity_mission = mission if isinstance(mission, dict) and mission else context_mission
    metadata = identity_mission.get("metadata") if isinstance(identity_mission, dict) and isinstance(identity_mission.get("metadata"), dict) else {}
    conversation_id = (
        conversation_id
        or str((conversation or {}).get("conversation_id") or "").strip()
        or str((mission or {}).get("conversation_id") or "").strip()
        or mission_id
    )
    if not conversation_id:
        return _err(rid, 4006, "conversation_id required")
    conversation_session_id = (
        conversation_session_id
        or str((conversation or {}).get("stable_session_id") or "").strip()
        or (_team_conversation_session_id(mission) if mission else "")
    )
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    params = {
        **params,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        **({"mission_id": mission_id, "missionId": mission_id} if mission_id else {}),
    }
    archived_team_error = _archived_team_write_error(
        db,
        _team_id_for_profile(params, mission=identity_mission if isinstance(identity_mission, dict) else {}, conversation=conversation),
    )
    if archived_team_error:
        return _err(rid, 4023, archived_team_error)
    try:
        params, leader_runtime_context = _resolve_team_leader_runtime_params_for_request(
            params,
            (graph if isinstance(graph, dict) and graph else context_graph) if isinstance(context_graph, dict) else {},
            db,
        )
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    prompt_graph = (graph if isinstance(graph, dict) and graph else context_graph) if isinstance(context_graph, dict) else {}
    profile_params = _leader_profile_params(params, prompt_graph if isinstance(prompt_graph, dict) else {})
    runtime_scope_key = _leader_conversation_runtime_scope_key(
        params,
        conversation_id=conversation_id,
        mission_id=mission_id,
    )
    contract_error = _leader_conversation_runtime_scope_contract_error(params, runtime_scope_key)
    if contract_error:
        return _err(rid, 4094, contract_error)
    owner_error = _leader_runtime_owner_error(profile_params, leader_runtime_scope_key=runtime_scope_key)
    if owner_error:
        return _err(rid, 4094, owner_error)
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=identity_mission if isinstance(identity_mission, dict) else {},
            conversation=conversation if isinstance(conversation, dict) else {},
            session_id=conversation_session_id,
            require=True,
        )
        conversation_title = _conversation_title_from_submit(db, params, text)
        conversation = db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            stable_session_id=conversation_session_id,
            mission=mission if isinstance(mission, dict) and mission else {},
            mission_id=mission_id if isinstance(mission, dict) and mission else "",
            team_id=str(params.get("team_id") or params.get("teamId") or (identity_mission or {}).get("team_id") or ""),
            title=conversation_title,
            objective=str(params.get("objective") or params.get("prompt") or (identity_mission or {}).get("objective") or text),
            workspace_id=workspace_context["workspace_id"],
            workspace_path=workspace_context["workspace_path"],
            created_by_user_id=str(params.get("created_by_user_id") or params.get("createdByUserId") or (identity_mission or {}).get("created_by_user_id") or ""),
            metadata={"display_title_source": "first_user_message"} if conversation_title else None,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.message.submit",
                "conversation_id": conversation_id,
                **({"mission_id": mission_id} if mission_id else {}),
                "team_id": str(params.get("team_id") or params.get("teamId") or (identity_mission or {}).get("team_id") or ""),
            },
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team conversation session unavailable: {exc}")
    if not graph:
        graph = {
            "mission": mission if isinstance(mission, dict) else {},
            "conversation": conversation,
            "nodes": [],
            "edges": [],
            "run_bindings": [],
        }
    memory_context, memory_text = (
        _team_memory_for_leader_message(db, params, mission, objective=text)
        if isinstance(mission, dict) and mission
        else ({}, "")
    )
    run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    turn_id = str(params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex).strip()
    draft_text = str(params.get("draft_text") or params.get("draftText") or text)
    submitted_attachments = _submitted_attachments(params)
    direct_reply = _leader_message_requests_direct_reply(text)
    team_context = {
        "kind": "leader_conversation",
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "team_id": str(params.get("team_id") or params.get("teamId") or (identity_mission or {}).get("team_id") or (conversation or {}).get("team_id") or ""),
        "mode": (mission or {}).get("mode") or str(params.get("mode") or ""),
        "status": (mission or {}).get("status") or "",
        "workspace_id": workspace_context["workspace_id"],
        "workspace_path": workspace_context["workspace_path"],
        "memory": memory_context,
        "members": _leader_members_from_params(params, mission if isinstance(mission, dict) else {}),
        "tool_policy": _team_leader_tool_policy(surface="leader_conversation"),
    }
    snapshot_id = _team_capability_snapshot_id(params)
    if snapshot_id:
        team_context["team_capability_snapshot_id"] = snapshot_id
    if mission_id:
        team_context["mission_id"] = mission_id
    submit_params = {
        **params,
        **profile_params,
        "stored_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        "agent_context_mode": "team_leader",
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        "text": (
            _leader_direct_reply_prompt(
                user_text=text,
                graph=prompt_graph if isinstance(prompt_graph, dict) and prompt_graph else graph,
                memory_text=memory_text,
            )
            if direct_reply
            else _leader_router_prompt(user_text=text, graph=prompt_graph if isinstance(prompt_graph, dict) and prompt_graph else graph, memory_text=memory_text)
        ),
        "persist_user_message": draft_text,
        "draft_text": draft_text,
        "attachments": submitted_attachments,
        "enabled_toolsets": [] if direct_reply else _leader_message_toolsets(params),
        "disabled_toolsets": _leader_disabled_toolsets(params),
        "toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE,
        "dovie_product_context": {
            **(params.get("dovie_product_context") if isinstance(params.get("dovie_product_context"), dict) else {}),
            "team_mission": team_context,
        },
    }
    if direct_reply:
        submit_params["reasoning_config"] = dict(_TEAM_LEADER_DIRECT_REPLY_REASONING_CONFIG)
    runtime_session_error = _ensure_team_mission_runtime_session_shell(conversation_session_id)
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    response = _methods["run.submit"](rid, submit_params)
    if isinstance(response, dict) and response.get("error"):
        return response
    result = response.get("result") if isinstance(response, dict) else {}
    if isinstance(mission, dict) and mission and submitted_attachments:
        _record_leader_input_attachment_artifacts(
            db,
            mission=mission,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            attachments=submitted_attachments,
        )
    ensure_team_leader_message_run_state(db, run_id=run_id, session_id=conversation_session_id, runtime_scope_key=runtime_scope_key, result=result)
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "conversation": conversation,
            "leader_turn": result or {},
            "leader_runtime_context": leader_runtime_context,
            "leaderRuntimeContext": leader_runtime_context,
            "graph": db.get_team_mission_graph(mission_id) if mission_id else graph,
        },
    )


@method("team_mission.graph")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    conversation_id = _conversation_id_from_params(params)
    if conversation_id:
        graph = db.get_team_mission_conversation_graph(conversation_id)
        if not graph:
            return _err(rid, 4040, "team mission conversation not found")
        mission_ids = _graph_mission_ids(graph)
        if mission_id and mission_id not in mission_ids:
            return _err(rid, 4040, "team mission not found in conversation")
        graph_mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
        graph_mission_id = str(
            graph_mission.get("mission_id")
            or graph_mission.get("missionId")
            or ""
        ).strip()
        result = {
            "mission_id": graph_mission_id or mission_id,
            "conversation_id": conversation_id,
            "graph": graph,
        }
        if mission_id and mission_id != result["mission_id"]:
            result["requested_mission_id"] = mission_id
        return _ok(rid, result)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    graph = db.get_team_mission_graph(mission_id)
    if not graph:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, {"mission_id": mission_id, "graph": graph})


@method("team_mission.graph.reduce")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = db.reduce_team_mission_graph(mission_id)
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


@method("team_mission.events")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    limit = _bounded_limit(params.get("limit"), default=2000, maximum=10000)
    byte_limit = _bounded_byte_limit(params.get("byte_limit") or params.get("byteLimit"))
    raw_events = db.list_team_mission_run_events(
        mission_id,
        after_seq=after_seq,
        limit=min(limit + 1, 10000),
    )
    events, has_more, approx_event_bytes = _team_mission_event_page(
        raw_events,
        after_seq=after_seq,
        limit=limit,
        byte_limit=byte_limit,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "events": events,
            "last_event_seq": max([int(event.get("seq") or 0) for event in events], default=after_seq),
            "has_more": has_more,
            "approx_event_bytes": approx_event_bytes,
        },
    )


@method("team_mission.subscribe")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    # Stale-run watchdog: clear any run left active under an already-terminal
    # mission (cancel race / completion without a node terminal event / a run
    # that stalled on a live gateway). Self-heals the spinning card + lingering
    # runtime state when the conversation is opened/refreshed.
    reaper = getattr(db, "reap_terminal_mission_runs", None)
    if callable(reaper):
        try:
            reaper(mission_id)
        except Exception:
            pass
    # Cap canonical-log growth: prune per-token stream deltas of a terminal
    # mission (unbounded team_mission_events bloated the DB into the GBs, slowing
    # session-list loads into timeouts). Lazy, idempotent, terminal-only.
    pruner = getattr(db, "prune_team_mission_events", None)
    if callable(pruner):
        try:
            pruner(mission_id)
        except Exception:
            pass
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    limit = _bounded_limit(params.get("limit"), default=2000, maximum=10000)
    byte_limit = _bounded_byte_limit(params.get("byte_limit") or params.get("byteLimit"))
    subscription_id, raw_events = run_control.subscribe_team_mission_with_id(
        mission_id=mission_id,
        transport=current_transport(),
        after_seq=after_seq,
        limit=min(limit + 1, 10000),
        db=db,
    )
    events, has_more, approx_event_bytes = _team_mission_event_page(
        raw_events,
        after_seq=after_seq,
        limit=limit,
        byte_limit=byte_limit,
    )
    last_event_seq = max([int(event.get("seq") or 0) for event in events], default=after_seq)
    _log.info(
        "team_mission.subscribe replay mission_id=%s after_seq=%s limit=%s byte_limit=%s subscription_id=%s raw_count=%s replay_count=%s last_seq=%s has_more=%s approx_event_bytes=%s",
        mission_id,
        after_seq,
        limit,
        byte_limit,
        subscription_id,
        len(raw_events),
        len(events),
        last_event_seq,
        has_more,
        approx_event_bytes,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "subscription_id": subscription_id,
            "events": events,
            "last_event_seq": last_event_seq,
            "has_more": has_more,
            "approx_event_bytes": approx_event_bytes,
        },
    )


@method("team_mission.node.create")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    payload = _node_payload_from_params(params)
    node_id = str(payload.get("node_id") or payload.get("nodeId") or payload.get("id") or "").strip()
    if not node_id:
        return _err(rid, 4006, "node_id required")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return _err(rid, 4040, "team mission not found")
    metadata = payload.get("metadata") or {}
    output_contract = payload.get("output_contract") or payload.get("outputContract") or {}
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "node.metadata must be an object")
    if not isinstance(output_contract, dict):
        return _err(rid, 4004, "node.output_contract must be an object")
    metadata = dict(metadata)
    assignee_member_id = str(payload.get("assignee_member_id") or payload.get("assigneeMemberId") or "").strip()
    assignee_role = str(payload.get("assignee_role") or payload.get("assigneeRole") or "").strip()
    if assignee_member_id:
        metadata.setdefault("assignee_member_id", assignee_member_id)
    if assignee_role:
        metadata.setdefault("assignee_role", assignee_role)
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(payload.get("kind") or "worker"),
        title=str(payload.get("title") or ""),
        objective=str(payload.get("objective") or ""),
        status=str(payload.get("status") or "ready"),
        assignee_profile_id=str(payload.get("assignee_profile_id") or payload.get("assigneeProfileId") or ""),
        assignee_profile_version_id=str(
            payload.get("assignee_profile_version_id") or payload.get("assigneeProfileVersionId") or ""
        ),
        runtime_scope_key=str(payload.get("runtime_scope_key") or payload.get("runtimeScopeKey") or ""),
        output_contract=output_contract,
        metadata=metadata,
        position_x=float(payload.get("position_x") or payload.get("x") or 0),
        position_y=float(payload.get("position_y") or payload.get("y") or 0),
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.node.created", "payload": {"node": node}},
        )
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    node_metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    if isinstance(mission, dict) and _is_root_planning_node(node):
        mission = _activate_mission_task(db, mission, node, source="team_mission.node.create")
        graph = db.get_team_mission_graph(mission_id)
    if (
        isinstance(mission, dict)
        and _is_root_planning_node(node)
        and not _falsey(params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"))
    ):
        _append_team_user_task_message(
            db,
            mission=mission,
            objective=str(node.get("objective") or node.get("title") or ""),
            node_id=str(node.get("node_id") or ""),
            task_id=str(node_metadata.get("submitted_task_id") or node_metadata.get("task_id") or ""),
        )
    return _ok(rid, {"mission_id": mission_id, "node": node, "graph": graph})


@method("team_mission.edge.create")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    payload = _edge_payload_from_params(params)
    from_node_id = str(payload.get("from_node_id") or payload.get("fromNodeId") or payload.get("source") or "").strip()
    to_node_id = str(payload.get("to_node_id") or payload.get("toNodeId") or payload.get("target") or "").strip()
    if not from_node_id or not to_node_id:
        return _err(rid, 4006, "from_node_id and to_node_id required")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "edge.metadata must be an object")
    edge = db.upsert_team_mission_edge(
        mission_id=mission_id,
        edge_id=str(payload.get("edge_id") or payload.get("edgeId") or payload.get("id") or ""),
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        kind=str(payload.get("kind") or "depends_on"),
        metadata=metadata,
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.edge.created", "payload": {"edge": edge}},
        )
    return _ok(rid, {"mission_id": mission_id, "edge": edge, "graph": db.get_team_mission_graph(mission_id)})


@method("team_mission.node.update")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    payload = _node_payload_from_params(params)
    node_id = str(payload.get("node_id") or payload.get("nodeId") or payload.get("id") or "").strip()
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    existing = db.get_team_mission_node(mission_id, node_id)
    if not existing:
        return _err(rid, 4040, "team mission node not found")
    metadata = dict(existing.get("metadata") or {})
    if isinstance(payload.get("metadata"), dict):
        metadata.update(payload.get("metadata") or {})
    assignee_member_id = str(payload.get("assignee_member_id") or payload.get("assigneeMemberId") or "").strip()
    assignee_role = str(payload.get("assignee_role") or payload.get("assigneeRole") or "").strip()
    if assignee_member_id:
        metadata.setdefault("assignee_member_id", assignee_member_id)
    if assignee_role:
        metadata.setdefault("assignee_role", assignee_role)
    output_contract = dict(existing.get("output_contract") or {})
    if isinstance(payload.get("output_contract") or payload.get("outputContract"), dict):
        output_contract.update(payload.get("output_contract") or payload.get("outputContract") or {})
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(payload.get("kind") or existing.get("kind") or "worker"),
        title=str(payload.get("title") or existing.get("title") or ""),
        objective=str(payload.get("objective") or existing.get("objective") or ""),
        status=str(payload.get("status") or existing.get("status") or "todo"),
        assignee_profile_id=str(
            payload.get("assignee_profile_id") or payload.get("assigneeProfileId") or existing.get("assignee_profile_id") or ""
        ),
        assignee_profile_version_id=str(
            payload.get("assignee_profile_version_id")
            or payload.get("assigneeProfileVersionId")
            or existing.get("assignee_profile_version_id")
            or ""
        ),
        runtime_scope_key=str(payload.get("runtime_scope_key") or payload.get("runtimeScopeKey") or existing.get("runtime_scope_key") or ""),
        output_contract=output_contract,
        metadata=metadata,
        position_x=float(payload.get("position_x") or payload.get("x") or existing.get("position_x") or 0),
        position_y=float(payload.get("position_y") or payload.get("y") or existing.get("position_y") or 0),
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.node.updated", "payload": {"node": node}},
        )
    schedule_result = {}
    if str(node.get("status") or "") in {"completed", "verified"}:
        schedule_result = _schedule_ready_nodes(
            db=db,
            rid=rid,
            params={"mission_id": mission_id},
            trigger="node.update",
        )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "node": node,
            "scheduled": schedule_result,
            "graph": (schedule_result.get("graph") if isinstance(schedule_result, dict) else None) or db.get_team_mission_graph(mission_id),
        },
    )


@method("team_mission.plan.complete")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else None
    if not isinstance(mission, dict):
        return _err(rid, 4040, "team mission not found")
    result = db.complete_team_mission_plan(
        mission_id=mission_id,
        run_id=_run_id_from_params(params),
        task_id=str(params.get("task_id") or params.get("taskId") or ""),
        event_source="plan.complete",
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    schedule_result = {}
    if bool(result.get("auto_start_ready_nodes")):
        schedule_result = _schedule_ready_nodes(
            db=db,
            rid=rid,
            params={"mission_id": mission_id},
            trigger="plan.complete",
        )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "mission_status": result.get("mission_status") or "",
            "approval_requests": list(result.get("approval_requests") or []),
            "auto_start_ready_nodes": bool(result.get("auto_start_ready_nodes")),
            "scheduled": schedule_result,
            "graph": (schedule_result.get("graph") if isinstance(schedule_result, dict) else None) or result.get("graph") or {},
        },
    )


@method("team_mission.plan.approve")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else None
    if not isinstance(mission, dict):
        return _err(rid, 4040, "team mission not found")
    task_id = str(params.get("task_id") or params.get("taskId") or "").strip()
    approval_nodes: list[dict] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("kind") or "").strip() != "approval_gate":
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        node_task_id = str(
            node.get("task_id")
            or node.get("taskId")
            or metadata.get("task_id")
            or metadata.get("taskId")
            or ""
        ).strip()
        if task_id and node_task_id and node_task_id != task_id:
            continue
        approval_nodes.append(node)
    waiting_nodes = [
        node
        for node in approval_nodes
        if str(node.get("status") or "").strip() == "waiting_approval"
    ]
    if not waiting_nodes:
        mission_status = str(mission.get("status") or "").strip()
        completed_nodes = [
            node
            for node in approval_nodes
            if str(node.get("status") or "").strip() in {"completed", "verified"}
        ]
        if completed_nodes and mission_status != "waiting_approval":
            return _ok(
                rid,
                {
                    "mission_id": mission_id,
                    "already_approved": True,
                    "node": sorted(
                        completed_nodes,
                        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
                    )[-1],
                    "scheduled": {},
                    "graph": graph,
                },
            )
        return _err(rid, 4040, "team mission approval gate not found")
    approval_node = sorted(
        waiting_nodes,
        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
    )[-1]
    node_id = str(approval_node.get("node_id") or approval_node.get("id") or "").strip()
    if not node_id:
        return _err(rid, 4040, "team mission approval gate not found")
    metadata = dict(approval_node.get("metadata") or {})
    metadata.update({
        "approved_by": str(params.get("approved_by") or params.get("approvedBy") or "user").strip() or "user",
    })
    approved_node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(approval_node.get("kind") or "approval_gate"),
        title=str(approval_node.get("title") or "审批任务图"),
        objective=str(approval_node.get("objective") or ""),
        status="completed",
        assignee_profile_id=str(approval_node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(approval_node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=str(approval_node.get("runtime_scope_key") or ""),
        output_contract=dict(approval_node.get("output_contract") or {}),
        metadata=metadata,
        position_x=float(approval_node.get("position_x") or 0),
        position_y=float(approval_node.get("position_y") or 0),
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.plan.approved", "payload": {"node": approved_node}},
        )
    schedule_result = _schedule_ready_nodes(
        db=db,
        rid=rid,
        params={"mission_id": mission_id, "task_id": task_id, "limit": params.get("limit")},
        trigger="team_mission.plan.approve",
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "node": approved_node,
            "scheduled": schedule_result,
            "graph": (schedule_result.get("graph") if isinstance(schedule_result, dict) else None)
            or db.get_team_mission_graph(mission_id),
        },
    )


@method("team_mission.plan.reject")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = db.reject_team_mission_plan(
        mission_id=mission_id,
        task_id=str(params.get("task_id") or params.get("taskId") or ""),
        rejected_by=str(params.get("rejected_by") or params.get("rejectedBy") or "user"),
        reason=str(params.get("reason") or ""),
        run_id=_run_id_from_params(params),
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "task_id": result.get("task_id") or "",
            "canceled_nodes": list(result.get("canceled_nodes") or []),
            "graph": result.get("graph") or {},
        },
    )


@method("team_mission.cancel")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    reason = str(params.get("reason") or "").strip() or "Team Mission cancelled by user."
    result = db.cancel_team_mission(
        mission_id=mission_id,
        canceled_by=str(params.get("canceled_by") or params.get("canceledBy") or "user"),
        reason=reason,
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    canceled_runs: list[dict] = []
    cancel_errors: list[dict] = []
    seen_run_ids: set[str] = set()
    for binding in result.get("cancel_run_bindings") or []:
        if not isinstance(binding, dict):
            continue
        run_id = str(binding.get("run_id") or "").strip()
        if not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        stored_session_id = str(binding.get("session_id") or binding.get("stored_session_id") or "").strip()
        cancel_params = {
            "run_id": run_id,
            "stored_session_id": stored_session_id,
            "runtime_session_id": str(binding.get("runtime_session_id") or ""),
            "runtime_scope_key": str(binding.get("runtime_scope_key") or stored_session_id),
            "reason": reason,
        }
        try:
            response = _methods["run.cancel"](rid, cancel_params)
        except Exception as exc:
            cancel_errors.append({"run_id": run_id, "message": str(exc)})
            continue
        if isinstance(response, dict) and response.get("error"):
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            cancel_errors.append({
                "run_id": run_id,
                "message": str(error.get("message") or response.get("error") or "run cancel failed"),
            })
            continue
        response_result = response.get("result") if isinstance(response, dict) and isinstance(response.get("result"), dict) else {}
        canceled_runs.append({
            "run_id": run_id,
            "stored_session_id": stored_session_id,
            "status": str(response_result.get("status") or "cancelled"),
            "turn_id": str(response_result.get("turn_id") or ""),
        })
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "mission_status": result.get("mission_status") or "cancelled",
            "canceled_nodes": list(result.get("canceled_nodes") or []),
            "canceled_runs": canceled_runs,
            "cancel_errors": cancel_errors,
            "graph": db.get_team_mission_graph(mission_id),
        },
    )


@method("team_mission.node.bind_run")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    node_id = _node_id_from_params(params)
    run_id = _run_id_from_params(params)
    stored_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    if not run_id or not stored_session_id:
        return _err(rid, 4006, "run_id and stored_session_id required")
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return _err(rid, 4040, "team mission node not found")
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or node.get("runtime_scope_key")
        or stored_session_id
    ).strip()
    binding = db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=stored_session_id,
        runtime_session_id=str(params.get("runtime_session_id") or params.get("runtimeSessionId") or ""),
        runtime_scope_key=runtime_scope_key,
        role=str(params.get("role") or node.get("kind") or "worker"),
        metadata={"source": "team_mission.node.bind_run"},
    )
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(node.get("kind") or "worker"),
        title=str(node.get("title") or ""),
        objective=str(node.get("objective") or ""),
        status=str(params.get("status") or "running"),
        assignee_profile_id=str(node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=runtime_scope_key,
        output_contract=dict(node.get("output_contract") or {}),
        metadata={**dict(node.get("metadata") or {}), "stored_session_id": stored_session_id, "run_id": run_id},
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=run_id,
        event={
            "type": "mission.node.run.bound",
            "payload": {"node": node, "binding": binding},
        },
    )
    return _ok(rid, {"mission_id": mission_id, "node": node, "binding": binding})


@method("team_mission.node.start")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    node_id = _node_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return _err(rid, 4040, "team mission node not found")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = dict(node.get("metadata") or {})
    mission_metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    conversation_id = _conversation_id_from_params(params, mission_metadata) or str((mission or {}).get("conversation_id") or mission_id)
    conversation_session_id = _conversation_session_id_from_params(params, mission_metadata) or _team_conversation_session_id(mission if isinstance(mission, dict) else {})
    if isinstance(mission, dict) and mission:
        db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            stable_session_id=conversation_session_id,
            mission=mission,
        )
    if isinstance(mission, dict) and _is_root_planning_node(node):
        mission = _activate_mission_task(db, mission, node, source="team_mission.node.start")
        graph = db.get_team_mission_graph(mission_id)
    if (
        isinstance(mission, dict)
        and _is_root_planning_node(node)
        and not _falsey(params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"))
    ):
        _append_team_user_task_message(
            db,
            mission=mission,
            objective=str(node.get("objective") or node.get("title") or mission.get("objective") or ""),
            node_id=node_id,
            task_id=str(metadata.get("submitted_task_id") or metadata.get("task_id") or ""),
        )
    stored_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or metadata.get("stored_session_id")
        or _default_node_session_id(mission_id, node_id)
    ).strip()
    try:
        profile_params = _node_profile_params(params, mission if isinstance(mission, dict) else {}, node, db=db)
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or profile_params.get("runtime_scope_key")
        or node.get("runtime_scope_key")
        or stored_session_id
    ).strip()
    run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    turn_id = str(params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex).strip()
    if not db.get_session(stored_session_id):
        db.create_session(stored_session_id, source="team_mission", transient=False)
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=mission if isinstance(mission, dict) else {},
            session_id=stored_session_id,
            require=True,
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    bind_team_mission_session_workspace(
        session_id=stored_session_id,
        context=workspace_context,
        metadata={
            "source": "team_mission.node.start",
            "conversation_id": conversation_id,
            "mission_id": mission_id,
            "node_id": node_id,
            "team_id": str((mission or {}).get("team_id") or ""),
        },
    )
    runtime_session_error = _ensure_team_mission_runtime_session_shell(stored_session_id)
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    text = _strategy_start_text(params, mission if isinstance(mission, dict) else {}, node)
    memory_context, memory_text = _team_memory_for_node(
        db,
        params,
        mission if isinstance(mission, dict) else {},
        node,
        objective=text,
    )
    if memory_text:
        text = f"{text}\n\n{memory_text}"
    enabled_toolsets = _start_toolsets(
        params,
        mission if isinstance(mission, dict) else {},
        node,
        profile_params=profile_params,
        db=db,
    )
    leader_control_node = _is_team_leader_control_node(node)
    agent_profile_id = str(
        profile_params.get("agent_profile_id")
        or params.get("agent_profile_id")
        or params.get("agentProfileId")
        or node.get("assignee_profile_id")
        or ""
    ).strip()
    agent_profile_version_id = str(
        profile_params.get("agent_profile_version_id")
        or params.get("agent_profile_version_id")
        or params.get("agentProfileVersionId")
        or node.get("assignee_profile_version_id")
        or ""
    ).strip()
    task_id = str(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
        or ""
    ).strip()
    mission_metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    active_task = dict(mission_metadata.get("active_task") or {}) if isinstance(mission_metadata.get("active_task"), dict) else {}
    if not active_task and _is_root_planning_node(node):
        active_task = {
            "task_id": task_id,
            "title": str(node.get("title") or mission.get("title") or "").strip(),
            "objective": str(node.get("objective") or mission.get("objective") or "").strip(),
            "root_node_id": node_id,
        }
    binding_metadata = {"turn_id": turn_id, "source": "team_mission.node.start"}
    if task_id:
        binding_metadata["task_id"] = task_id
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=stored_session_id,
        runtime_session_id="",
        runtime_scope_key=runtime_scope_key,
        role=_node_role(node),
        metadata={**binding_metadata, "prebound": True},
    )
    submit_params = {
        **params,
        **profile_params,
        "stored_session_id": stored_session_id,
        "session_id": stored_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        "agent_profile_id": agent_profile_id,
        "agent_profile_version_id": agent_profile_version_id,
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        "text": text,
        "enabled_toolsets": enabled_toolsets,
        **({"disabled_toolsets": _leader_disabled_toolsets(params)} if leader_control_node else {}),
        **({"toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE} if leader_control_node or enabled_toolsets else {}),
        "dovie_product_context": {
            **(params.get("dovie_product_context") if isinstance(params.get("dovie_product_context"), dict) else {}),
            "team_mission": {
                "kind": "leader_planning_node" if leader_control_node else "mission_node",
                "surface": "mission_node",
                "mission_id": mission_id,
                "conversation_id": conversation_id,
                "conversation_session_id": conversation_session_id,
                "parent_conversation_session_id": conversation_session_id,
                "workspace_id": workspace_context["workspace_id"],
                "workspace_path": workspace_context["workspace_path"],
                "node_id": node_id,
                "node_title": node.get("title") or "",
                "node_kind": node.get("kind") or "",
                "node_role": _node_role(node),
                "node_phase": _node_phase(node),
                "active_task": active_task,
                "output_contract": node.get("output_contract") or {},
                "memory": memory_context,
                "delegate_inherits_parent_tools": not leader_control_node,
                **({"tool_policy": _team_leader_tool_policy(surface="leader_node")} if leader_control_node else {}),
            },
        },
    }
    response = _methods["run.submit"](rid, submit_params)
    if isinstance(response, dict) and response.get("error"):
        node = db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id=node_id,
            kind=str(node.get("kind") or "worker"),
            title=str(node.get("title") or ""),
            objective=str(node.get("objective") or ""),
            status="blocked",
            assignee_profile_id=str(node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
            runtime_scope_key=runtime_scope_key,
            output_contract=dict(node.get("output_contract") or {}),
            metadata={
                **metadata,
                "stored_session_id": stored_session_id,
                "start_error": response.get("error", {}).get("message") or "",
                "effective_toolsets": enabled_toolsets,
            },
            position_x=float(node.get("position_x") or 0),
            position_y=float(node.get("position_y") or 0),
        )
        return response
    result = response.get("result") if isinstance(response, dict) else {}
    result_run_id = str((result or {}).get("run_id") or run_id).strip()
    result_turn_id = str((result or {}).get("turn_id") or turn_id).strip()
    result_scope = str((result or {}).get("runtime_scope_key") or runtime_scope_key).strip()
    binding_metadata["turn_id"] = result_turn_id
    binding = db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=result_run_id,
        session_id=stored_session_id,
        runtime_session_id=str((result or {}).get("session_id") or ""),
        runtime_scope_key=result_scope,
        role=_node_role(node),
        metadata=binding_metadata,
    )
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(node.get("kind") or "worker"),
        title=str(node.get("title") or ""),
        objective=str(node.get("objective") or ""),
        status="running",
        assignee_profile_id=str(node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=result_scope,
        output_contract=dict(node.get("output_contract") or {}),
        metadata={
            **metadata,
            "stored_session_id": stored_session_id,
            "run_id": result_run_id,
            "turn_id": result_turn_id,
            "effective_toolsets": enabled_toolsets,
        },
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )
    if (
        isinstance(mission, dict)
        and str(mission.get("status") or "") == "draft"
        and str(node.get("kind") or "") == "root"
        and _node_phase(node) == "planning"
    ):
        db.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="planning",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata=dict(mission.get("metadata") or {}),
        )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=result_run_id,
        event={
            "type": "mission.node.started",
            "payload": {"node": node, "binding": binding},
        },
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "node": node,
            "binding": binding,
            "run": result,
            "stored_session_id": stored_session_id,
        },
    )


@method("team_mission.schedule.ready")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = _schedule_ready_nodes(
        db=db,
        rid=rid,
        params=params,
        trigger="team_mission.schedule.ready",
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


@method("team_mission.memory.compile")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        result = db.compile_team_mission_memory(
            mission_id=mission_id,
            task_id=str(params.get("task_id") or params.get("taskId") or ""),
            mode=str(params.get("mode") or "final"),
            source_run_ids=_normalize_toolsets(params.get("source_run_ids") or params.get("sourceRunIds")),
            emit_event=not _falsey(params.get("emit_event") if "emit_event" in params else params.get("emitEvent")),
        )
    except Exception as exc:
        return _err(rid, 5008, f"team mission memory compile failed: {exc}")
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


@method("team_mission.memory.pack")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = db.build_team_mission_memory_pack(
        mission_id=mission_id,
        objective=str(params.get("objective") or ""),
        workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""),
        limit=_bounded_limit(params.get("limit"), default=8, maximum=50),
        include_team_scope=_team_memory_include_team_scope(params, {}),
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


@method("team_mission.memory.slice")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    node_id = _node_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    result = db.build_team_mission_memory_slice(
        mission_id=mission_id,
        node_id=node_id,
        objective=str(params.get("objective") or ""),
        limit=_bounded_limit(params.get("limit"), default=5, maximum=50),
        include_team_scope=_team_memory_include_team_scope(params, {}),
    )
    if not result:
        return _err(rid, 4040, "team mission or node not found")
    return _ok(rid, result)


@method("team_mission.memory.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    items = db.list_team_mission_memory_items(
        mission_id=_mission_id_from_params(params),
        conversation_session_id=str(
            params.get("conversation_session_id")
            or params.get("conversationSessionId")
            or ""
        ),
        team_id=str(params.get("team_id") or params.get("teamId") or ""),
        task_id=str(params.get("task_id") or params.get("taskId") or ""),
        kinds=_normalize_toolsets(params.get("kinds") or params.get("kind")),
        statuses=_normalize_toolsets(params.get("statuses") or params.get("status")),
        visibility=_normalize_toolsets(params.get("visibility")),
        include_deleted=bool(params.get("include_deleted") or params.get("includeDeleted")),
        limit=_bounded_limit(params.get("limit"), default=200, maximum=1000),
    )
    return _ok(rid, {"items": items})


@method("team_mission.memory.update")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    memory_id = str(params.get("memory_id") or params.get("memoryId") or params.get("id") or "").strip()
    if not memory_id:
        return _err(rid, 4006, "memory_id required")
    structured_payload = params.get("structured_payload") or params.get("structuredPayload")
    if structured_payload is not None and not isinstance(structured_payload, dict):
        return _err(rid, 4004, "structured_payload must be an object")
    try:
        item = db.update_team_mission_memory_item(
            memory_id,
            content=params.get("content") if "content" in params else None,
            structured_payload=structured_payload,
            visibility=params.get("visibility") if "visibility" in params else None,
            status=params.get("status") if "status" in params else None,
            confidence=float(params["confidence"]) if "confidence" in params else None,
        )
    except Exception as exc:
        return _err(rid, 5008, f"team mission memory update failed: {exc}")
    if not item:
        return _err(rid, 4040, "team mission memory item not found")
    return _ok(rid, {"item": item})


@method("team_mission.memory.delete")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    memory_id = str(params.get("memory_id") or params.get("memoryId") or params.get("id") or "").strip()
    if not memory_id:
        return _err(rid, 4006, "memory_id required")
    item = db.delete_team_mission_memory_item(memory_id)
    if not item:
        return _err(rid, 4040, "team mission memory item not found")
    return _ok(rid, {"item": item})


@method("team_mission.memory.events")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    events = [
        event for event in db.list_team_mission_run_events(
            mission_id,
            after_seq=after_seq,
            limit=_bounded_limit(params.get("limit"), default=2000, maximum=10000),
        )
        if str((event or {}).get("type") or "").startswith("mission.memory.")
    ]
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "events": events,
            "last_event_seq": max([int(event.get("seq") or 0) for event in events], default=after_seq),
        },
    )
