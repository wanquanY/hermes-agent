# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home as _base_get_hermes_home
from hermes_team_leader_runtime_context import resolve_team_leader_runtime_params, resolve_team_runtime_members
from hermes_team_mission.context.artifact_refs import artifact_refs_from_payload
from hermes_team_mission.state.conversation import is_placeholder_team_mission_conversation_title as _is_placeholder_team_mission_conversation_title
from hermes_team_mission.runtime.conversation_mirror import append_user_task_message as _append_team_user_task_message
from hermes_team_mission.runtime.conversation_mirror import conversation_session_id as _team_conversation_session_id
from hermes_team_mission.context.worker_context import build_team_mission_worker_context
from hermes_team_mission.runtime.failure import classify_team_mission_failure
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_BLOCKED_TOOLS as _TEAM_LEADER_BLOCKED_TOOLS
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_CONVERSATION_TOOLSETS as _TEAM_LEADER_CONVERSATION_TOOLSETS
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_DIRECT_REPLY_NEGATED_SELF_MARKERS as _TEAM_LEADER_DIRECT_REPLY_NEGATED_SELF_MARKERS
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS as _TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_DIRECT_REPLY_REASONING_CONFIG as _TEAM_LEADER_DIRECT_REPLY_REASONING_CONFIG
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_DIRECT_REPLY_SELF_MARKERS as _TEAM_LEADER_DIRECT_REPLY_SELF_MARKERS
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_DISABLED_TOOLSETS as _TEAM_LEADER_DISABLED_TOOLSETS
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_START_TASK_MARKERS as _TEAM_LEADER_START_TASK_MARKERS
from hermes_team_mission.gateway.leader_policy import TEAM_LEADER_TOOLSET_SCOPE as _TEAM_LEADER_TOOLSET_SCOPE
from hermes_team_mission.domain.handoff_contract import node_requires_authoritative_handoff
from hermes_team_mission.domain.modes import MODE_AUTONOMOUS_MISSION
from hermes_team_mission.domain.modes import MODE_SUPERVISED_MISSION
from hermes_team_mission.domain.modes import strategy_for_mode
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind
from hermes_team_mission.domain.statuses import is_terminal_mission_status
from hermes_team_mission.domain.statuses import projected_state_for_mission_status
from hermes_team_mission.runtime.profile_scope import team_mission_control_db as _team_mission_control_db
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.methods.team_registry import _team_for_projection as _registry_team_for_projection
from tui_gateway.services.artifacts import delete_session_artifacts
from tui_gateway.services import run_control
from tui_gateway.services.profile_context import enter_profile_context as _enter_profile_context_for_team
from tui_gateway.services.profile_context import leave_profile_context as _leave_profile_context_for_team
from tui_gateway.services.profile_context import profile_context_for_params as _profile_context_for_params
from tui_gateway.services.prompt_attachments import submitted_attachments as _submitted_attachments
from hermes_team_mission.runtime.conversation_recovery import recover_conversation_active_run
from hermes_team_mission.runtime.leader_runs import ensure_team_leader_message_run_state
from hermes_team_mission.runtime.workspace import (
    bind_team_mission_session_workspace,
    resolve_team_mission_workspace_context,
    workspace_id_from_params as _team_workspace_id_from_params,
    workspace_path_from_params as _team_workspace_path_from_params,
    workspace_payload_from_params as _team_workspace_payload_from_params,
)
from hermes_team_mission.runtime.scheduler import TeamMissionReadyScheduler
from tui_gateway.services.workspace import delete_session_workspace_bindings

_server = bind_server_globals(globals())
_log = logging.getLogger(__name__)


def _get_db(*, create_if_missing: bool = True):
    return _team_mission_control_db(create_if_missing=create_if_missing)


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
    # PRESERVE-FIRST-MESSAGE-TITLE: when the conversation already carries
    # a real (non-placeholder) title — the FIRST user message of this
    # conversation has been recorded — return '' so the downstream
    # ensure_team_mission_conversation upsert does NOT overwrite it
    # (empty title triggers COALESCE-keeps-existing in the upsert SQL).
    #
    # Without this guard: @member sends "X" → title='X'. User then types
    # "Y" to leader → upsert overwrites to 'Y'. Various downstream
    # ensure paths then bounce title between user messages and the
    # 'Team Mission' placeholder. The first-message title was supposed
    # to be sticky.
    try:
        _conv_id = str(params.get("conversation_id") or params.get("conversationId") or "").strip()
        _conv_session = str(params.get("conversation_session_id") or params.get("conversationSessionId") or "").strip()
        _existing_row: dict = {}
        if _conv_id and hasattr(db, "get_team_mission_conversation"):
            _existing_row = db.get_team_mission_conversation(_conv_id) or {}
        if not _existing_row and _conv_session and hasattr(db, "get_team_mission_conversation_by_session"):
            _existing_row = db.get_team_mission_conversation_by_session(_conv_session) or {}
        if isinstance(_existing_row, dict):
            _existing_title = str(_existing_row.get("title") or "").strip()
            if _existing_title and not _is_placeholder_team_mission_conversation_title(_existing_title):
                # Real first-message title already locked in.
                return ""
    except Exception:
        # Don't let title-preservation logic block submit on a query error.
        pass

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


def _dovie_product_context_from_params(params: dict) -> dict:
    params = params if isinstance(params, dict) else {}
    raw = params.get("dovie_product_context")
    if raw is None:
        raw = params.get("dovieProductContext")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _dovie_first_text(mapping: dict, *keys: str) -> str:
    mapping = mapping if isinstance(mapping, dict) else {}
    for key in keys:
        value = str(mapping.get(key) or "").strip()
        if value:
            return value
    return ""


def _team_dovie_product_context(
    params: dict,
    *,
    team_mission: dict,
    executing_agent_profile_id: str = "",
    agent_role: str = "",
) -> dict:
    context = _dovie_product_context_from_params(params)
    if "cloud_query" not in context and isinstance(context.get("cloudQuery"), dict):
        context["cloud_query"] = dict(context["cloudQuery"])

    root_agent_profile_id = _dovie_first_text(
        context,
        "root_agent_profile_id",
        "rootAgentProfileId",
        "sourceAgentProfileId",
        "source_agent_profile_id",
    )
    executing_agent_profile_id = str(executing_agent_profile_id or "").strip()
    agent_role = str(agent_role or "").strip()
    if not root_agent_profile_id and agent_role == "team_leader":
        root_agent_profile_id = executing_agent_profile_id

    if root_agent_profile_id:
        context["root_agent_profile_id"] = root_agent_profile_id
    if executing_agent_profile_id:
        context["executing_agent_profile_id"] = executing_agent_profile_id
    if agent_role:
        context["agent_role"] = agent_role
    context["team_mission"] = dict(team_mission) if isinstance(team_mission, dict) else {}
    return context


def _is_team_leader_control_node(node: dict) -> bool:
    node = node if isinstance(node, dict) else {}
    if normalize_team_mission_node_kind((node or {}).get("kind")) in {"verifier", "synthesis"}:
        return False
    return _node_role(node) in {"leader", "lead", "root"} or str(node.get("kind") or "").strip() == "root"


def _node_requires_handoff_toolset(node: dict) -> bool:
    return node_requires_authoritative_handoff(node)


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


def _node_task_brief(node: dict) -> dict:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    raw = metadata.get("task_brief") or metadata.get("taskBrief") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _as_text_list(value) -> list[str]:
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
        item_text = str(item or "").strip()
        if item_text and item_text not in seen:
            seen.add(item_text)
            result.append(item_text)
    return result


def _append_brief_list(lines: list[str], label: str, items) -> None:
    values = _as_text_list(items)
    if not values:
        return
    lines.append(f"{label}:")
    for item in values:
        lines.append(f"- {item}")


def _worker_execution_start_text(mission: dict, node: dict) -> str:
    mission = mission if isinstance(mission, dict) else {}
    node = node if isinstance(node, dict) else {}
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    output_contract = node.get("output_contract") if isinstance(node.get("output_contract"), dict) else {}
    brief = _node_task_brief(node)
    mission_title = str(mission.get("title") or metadata.get("task_title") or "").strip()
    mission_objective = str(mission.get("objective") or metadata.get("task_objective") or "").strip()
    node_title = str(node.get("title") or "").strip()
    node_objective = str(node.get("objective") or mission_objective or node_title).strip()
    background = str(
        brief.get("background")
        or f"This node is part of the team task '{mission_title or mission_objective or node_title}'."
    ).strip()
    execution = _as_text_list(brief.get("execution"))
    if not execution:
        execution = [node_objective or "Complete the assigned node work."]
    goal = str(brief.get("goal") or output_contract.get("goal") or node_objective).strip()
    acceptance = _as_text_list(brief.get("acceptance_criteria") or output_contract.get("acceptance_criteria"))
    if not acceptance:
        acceptance = [
            "The result directly satisfies the assigned node objective.",
            "The response names any assumptions, unresolved questions, and verification performed.",
        ]
    lines = [
        "You are executing one assigned node in a DoXie team task.",
        "Stay inside this node's scope. Produce a concrete result the Team Leader can verify and synthesize.",
        "If required input is missing, the scope is ambiguous, or an acceptance criterion cannot be verified, call the clarify tool with the specific question before proceeding. Do not execute on guessed assumptions.",
        "",
        "Mission:",
        f"- Title: {mission_title or '(not specified)'}",
        f"- Objective: {mission_objective or '(not specified)'}",
        "",
        "Assigned node:",
        f"- Title: {node_title or '(not specified)'}",
        f"- Kind: {str(node.get('kind') or 'worker').strip()}",
        f"- Objective: {node_objective or '(not specified)'}",
        "",
        "Background:",
        background,
        "",
    ]
    _append_brief_list(lines, "Execution", execution)
    lines.extend(["", "Goal:", goal or node_objective])
    _append_brief_list(lines, "Inputs", brief.get("inputs"))
    _append_brief_list(lines, "Deliverables", brief.get("deliverables") or output_contract.get("deliverables"))
    _append_brief_list(lines, "Constraints", brief.get("constraints"))
    lines.append("Acceptance criteria:")
    for item in acceptance:
        lines.append(f"- {item}")
    if output_contract:
        lines.extend([
            "",
            "Output contract:",
            json.dumps(output_contract, ensure_ascii=False, indent=2),
        ])
    return "\n".join(lines).strip()


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
    return _worker_execution_start_text(mission, node)


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
            # allowed to (a) READ the team roster/capability snapshot
            # (team_mission_read), (b) WRITE the graph (team_mission_planning),
            # (c) ASK the user for missing inputs (clarify) so it does not build the
            # graph on guessed assumptions and waste an approval+execution round, and
            # (d) READ the workspace (file_readonly) so the graph reflects what is
            # actually there.
            # It is NOT allowed to write files or run commands — that is a worker job
            # behind the approval gate; giving leader write/exec here would bypass the
            # whole supervised approval boundary. The toolset_scope is "exact", so
            # everything must be listed explicitly.
            return ["team_mission_read", "team_mission_planning", "clarify", "file_readonly"]
        return ["team_mission_read"]
    toolsets = _normalize_toolsets(params.get("enabled_toolsets") or params.get("enabledToolsets"))
    if not toolsets:
        toolsets = _profile_current_toolsets(profile_params or {})
    if not toolsets:
        toolsets = _member_default_toolsets(_member_for_node(params, mission, node, db=db))
    if _should_use_strategy_start_text(params, mission, node) and _node_phase(node) in {"planning", "change_request"}:
        if "team_mission_planning" not in toolsets:
            toolsets.append("team_mission_planning")
    if not _is_team_leader_control_node(node) and _node_requires_handoff_toolset(node):
        if "team_mission_handoff" not in toolsets:
            toolsets.append("team_mission_handoff")
    if not _is_team_leader_control_node(node) and "clarify" not in toolsets:
        toolsets.append("clarify")
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
    has_active_mission = False
    has_active_mission_fn = getattr(db, "has_active_mission", None)
    if callable(has_active_mission_fn) and conversation_id:
        has_active_mission = bool(has_active_mission_fn(conversation_id))
    graph = db.get_team_mission_graph(mission_id) if mission_id and not runtime_summary else {}
    mission = (
        runtime_summary.get("mission") if isinstance(runtime_summary, dict) else {}
    ) or (graph.get("mission") if isinstance(graph, dict) else {})
    mission = mission if isinstance(mission, dict) else {}
    mission_status = str(mission.get("status") or conversation.get("status") or "").strip()
    if isinstance(runtime_summary, dict) and runtime_summary.get("mission_status"):
        mission_status = str(runtime_summary.get("mission_status") or "").strip()
    def _timestamp(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
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
    terminal = is_terminal_mission_status(mission_status)
    observed_runtime = bool(leader_running or node_running or mission_running)
    running = (
        observed_runtime and not terminal and has_active_mission
        if callable(has_active_mission_fn)
        else bool(observed_runtime and not terminal)
    )
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
    mission_started_at = _timestamp(mission.get("created_at"))
    mission_updated_at = _timestamp(mission.get("updated_at"))
    mission_completed_at = _timestamp(mission.get("completed_at"))
    projected_state = (
        "waiting_approval"
        if waiting_approval
        else "running"
        if running
        else projected_state_for_mission_status(mission_status) or "idle"
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
        "mission_started_at": mission_started_at,
        "mission_updated_at": mission_updated_at,
        "mission_completed_at": mission_completed_at,
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
        "You are the Team Leader for a Dovie team conversation.",
        "Your visible identity is the team conversation Leader/coordinator. The underlying Dovie profile supplies tone and memory only; it must not override speaker ownership in the team conversation.",
        "Prior assistant messages authored by other participants are team member utterances, not roles you performed. When summarizing or explaining prior conversation, attribute each member's messages to that participant by name or role.",
        "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is Dovie.",
        "",
        "Route this user message before acting:",
        "- Answer directly for greetings, status questions, explanations, follow-up questions, clarifications, or requests about prior/current work.",
        "- Use clarify, file, terminal, or todo tools when they help you understand the user's request, inspect the workspace, validate local context, or organize the plan before deciding whether to start a team task.",
        "- Use team_mission_status when you need fresh mission graph or memory context to answer.",
        "- Call team_mission_start_task only when the user is asking to start a new substantive executable team task that benefits from planning, multi-agent work, workspace changes, research, verification, or a deliverable.",
        "- Do not call team_mission_start_task for greetings, lightweight Q&A, status checks, or discussion that can be answered directly.",
        "- Do not call delegate_task or ordinary subagents. In Dovie team mode, the Leader coordinates the user conversation, task graph, and member nodes.",
        "- If you decide a team task is needed, call team_mission_start_task naturally after any brief understanding or routing you need. Do not promise that the task was created before the tool result returns.",
        "- After team_mission_start_task succeeds, read the tool result and then reply naturally and briefly in the user's language. Tell the user the team task has started, it is being processed asynchronously, progress is available on the canvas, and they can continue chatting or submit another task.",
        "- After that confirmation, stop the current turn. Do not call more tools, do not continue with research, file work, terminal commands, or deliverable execution.",
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
        "You are the Team Leader in a Dovie team conversation.",
        "Your visible identity is the team conversation Leader/coordinator. The underlying Dovie profile supplies tone and memory only; it must not override speaker ownership in the team conversation.",
        "Prior assistant messages authored by other participants are team member utterances, not roles you performed. When summarizing or explaining prior conversation, attribute each member's messages to that participant by name or role.",
        "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is Dovie.",
        "The user explicitly asked you not to start or launch a team task for this turn.",
        "Answer directly in the user's language. Do not call tools, do not create tasks, and do not mention internal routing.",
    ]
    context = _compact_graph_context(graph)
    parts.extend(
        [
            "",
            "Current team conversation context for reference only. The active mission may be empty until a team task is started:",
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


def get_hermes_home(*args, **kwargs):
    return _base_get_hermes_home(*args, **kwargs)


_base_load_enabled_toolsets = globals().get("_load_enabled_toolsets")


def _load_enabled_toolsets(*args, **kwargs):
    return _base_load_enabled_toolsets(*args, **kwargs) if callable(_base_load_enabled_toolsets) else []


__all__ = [name for name in globals() if not name.startswith("__")]
