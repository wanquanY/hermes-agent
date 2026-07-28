"""Projection from canonical session-index rows to Desktop list items."""

from __future__ import annotations

import json

from tui_gateway.services.workspace import (
    session_workspace_bindings_by_session_ids,
)


def normalized_conversation_kind(row: dict | None) -> str:
    kind = str(
        (row or {}).get("conversation_kind")
        or (row or {}).get("conversationKind")
        or ""
    ).strip().lower()
    return kind if kind in {"direct", "team"} else "direct"


def _safe_json_decode(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _workspace_binding_contract(binding: object) -> dict | None:
    if not isinstance(binding, dict):
        return None
    workspace_id = str(binding.get("workspace_id") or binding.get("id") or "").strip()
    workspace = binding.get("workspace")
    workspace_path = str(
        binding.get("workspace_path")
        or binding.get("path")
        or (workspace.get("path") if isinstance(workspace, dict) else "")
        or ""
    ).strip()
    if not workspace_id and not workspace_path:
        return None
    return {
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
    }


def _team_context_contract(row: dict) -> dict | None:
    raw_context = row.get("team_context")
    if isinstance(raw_context, dict):
        context = {
            "team_id": str(raw_context.get("team_id") or "").strip(),
            "team_conversation_id": str(
                raw_context.get("team_conversation_id") or ""
            ).strip(),
            "mission_id": str(raw_context.get("mission_id") or "").strip(),
            "member_id": str(raw_context.get("member_id") or "").strip(),
        }
    else:
        context = {
            "team_id": str(
                row.get("team_context_team_id") or row.get("team_id") or ""
            ).strip(),
            "team_conversation_id": str(
                row.get("team_context_conversation_id")
                or row.get("conversation_id")
                or ""
            ).strip(),
            "mission_id": str(
                row.get("team_context_mission_id")
                or row.get("mission_id")
                or row.get("active_mission_id")
                or ""
            ).strip(),
            "member_id": str(row.get("team_context_member_id") or "").strip(),
        }
    return context if any(context.values()) else None


def _derived_state_contract(row: dict) -> dict:
    raw_state = row.get("derived_state")
    if isinstance(raw_state, dict):
        return {
            "running": bool(raw_state.get("running")),
            "waiting_approval": bool(raw_state.get("waiting_approval")),
            "terminal_status": (
                str(raw_state.get("terminal_status")).strip()
                if raw_state.get("terminal_status") is not None
                else None
            )
            or None,
        }
    return {
        "running": bool(row.get("derived_running", row.get("running"))),
        "waiting_approval": bool(
            row.get("derived_waiting_approval", row.get("waiting_approval"))
        ),
        "terminal_status": (
            str(row.get("derived_terminal_status")).strip()
            if row.get("derived_terminal_status") is not None
            else None
        )
        or None,
    }


def session_index_list_item(row: dict) -> dict:
    """Map a session-index row to the stable Desktop list-item shape."""

    session_kind = row.get("session_kind") or "hermes_session"
    conversation_kind = normalized_conversation_kind(row)
    is_team_conversation = conversation_kind == "team"
    active_mission_id = (
        row.get("active_mission_id")
        or row.get("team_conversation_active_mission_id")
        or row.get("mission_id")
        or ""
    )
    item = {
        "id": row.get("session_id") or "",
        "title": row.get("title") or "",
        "display_title": row.get("title") or "",
        "displayTitle": row.get("title") or "",
        "preview": row.get("preview") or "",
        "started_at": row.get("started_at") or 0,
        "updated_at": row.get("updated_at") or row.get("started_at") or 0,
        "message_count": row.get("message_count") or 0,
        "source": row.get("source") or "",
        "transient": bool(row.get("transient")),
        "session_kind": session_kind,
        "conversation_kind": conversation_kind,
        "agentProfileId": row.get("owner_agent_profile_id") or "",
        "agent_profile_id": row.get("owner_agent_profile_id") or "",
        "agentProfileVersionId": row.get("owner_profile_version_id") or "",
        "runtimeScopeKey": row.get("runtime_scope_key") or "",
        "runtime_scope_key": row.get("runtime_scope_key") or "",
        "status": row.get("status") or "",
        "running": bool(row.get("running")),
        "waiting_approval": bool(row.get("waiting_approval")),
        "pending_approval_count": row.get("pending_approval_count") or 0,
        "active_run_id": row.get("active_run_id") or "",
        "active_execution_session_id": row.get("active_execution_session_id") or "",
        "conversation_id": row.get("conversation_id") or "",
        "team_id": row.get("team_id") or "",
        "team_conversation_title": row.get("team_conversation_title") or "",
        "mission_id": row.get("mission_id") or active_mission_id or "",
        "active_mission_id": active_mission_id,
        "mission_status": row.get("mission_status") or "",
        "workspace_binding": _workspace_binding_contract(row.get("workspace_binding")),
        "team_context": _team_context_contract(row),
        "derived_state": _derived_state_contract(row),
    }
    if is_team_conversation and active_mission_id:
        item["mission_id"] = active_mission_id
    if is_team_conversation and row.get("team_id"):
        _append_team_projection(item, row)
    if row.get("conversation_id"):
        _append_conversation_projection(item, row)
    return item


def _append_team_projection(item: dict, row: dict) -> None:
    team_name = row.get("team_name") or ""
    team_avatar = _safe_json_decode(row.get("team_avatar_json"))
    lead_profile_id = row.get("team_lead_profile_id") or ""
    lead_profile_name = row.get("team_lead_profile_name") or ""
    lead_profile_avatar = row.get("team_lead_profile_avatar") or ""
    member_count = int(row.get("team_member_count") or 0)
    leader_member = _safe_json_decode(row.get("team_leader_member_json")) or {}
    if not isinstance(leader_member, dict):
        leader_member = {}
    display_members = _safe_json_decode(row.get("team_display_members_json")) or []
    if not isinstance(display_members, list):
        display_members = []
    if leader_member and not display_members:
        display_members = [leader_member]
    team_block = {"id": row.get("team_id") or "", "name": team_name}
    if team_avatar is not None:
        team_block["avatar"] = team_avatar
    if lead_profile_id:
        team_block["lead_agent_profile_id"] = lead_profile_id
        team_block["leadAgentProfileId"] = lead_profile_id
    if member_count:
        team_block["member_count"] = member_count
        team_block["memberCount"] = member_count
    if leader_member:
        team_block["leader_member"] = leader_member
        team_block["leaderMember"] = leader_member
    if display_members:
        team_block["display_members"] = display_members
        team_block["displayMembers"] = display_members
    item["team"] = team_block
    if team_name:
        item["team_name"] = team_name
        item["teamName"] = team_name
    if member_count:
        item["team_member_count"] = member_count
        item["teamMemberCount"] = member_count
    if lead_profile_name:
        item["lead_profile_name"] = lead_profile_name
        item["leadProfileName"] = lead_profile_name
    if lead_profile_avatar:
        item["lead_profile_avatar"] = lead_profile_avatar
        item["leadProfileAvatar"] = lead_profile_avatar


def _append_conversation_projection(item: dict, row: dict) -> None:
    objective = row.get("team_conversation_objective") or ""
    workspace_id = row.get("team_conversation_workspace_id") or ""
    workspace_path = row.get("team_conversation_workspace_path") or ""
    active_mission_id = row.get("team_conversation_active_mission_id") or ""
    if objective:
        item["objective"] = objective
    if workspace_id:
        item["workspace_id"] = workspace_id
        item["workspaceId"] = workspace_id
    if workspace_path:
        item["workspace_path"] = workspace_path
        item["workspacePath"] = workspace_path
    if active_mission_id and not item.get("active_mission_id"):
        item["active_mission_id"] = active_mission_id


def workspace_bindings_for_rows(rows: list[dict]) -> dict[str, dict]:
    session_ids = [
        str(row.get("session_id") or row.get("id") or "").strip()
        for row in rows
        if isinstance(row, dict)
    ]
    session_ids = [
        session_id
        for session_id in dict.fromkeys(session_ids)
        if session_id
    ]
    if not session_ids:
        return {}
    try:
        bindings = session_workspace_bindings_by_session_ids(session_ids)
    except Exception:
        return {}
    return bindings if isinstance(bindings, dict) else {}


def row_with_active_mission_running(db, row: dict) -> dict:
    item = dict(row or {})
    is_team_conversation = normalized_conversation_kind(item) == "team"
    conversation_id = str(item.get("conversation_id") or "").strip()
    has_active_mission = getattr(db, "has_active_mission", None)
    if is_team_conversation and conversation_id and callable(has_active_mission):
        item["running"] = bool(has_active_mission(conversation_id))
    return item


__all__ = [
    "normalized_conversation_kind",
    "row_with_active_mission_running",
    "session_index_list_item",
    "workspace_bindings_for_rows",
]
