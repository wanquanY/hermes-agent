"""Read model for the control-plane session index sidebar projection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionIndexQuery:
    limit: int = 200
    cursor: dict[str, Any] | None = None
    include_transient: bool = False
    conversation_kind: str | None = None


class SessionIndexReadModel:
    """SQLite-backed read model for session_index sidebar rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list(self, query: SessionIndexQuery) -> dict[str, Any]:
        capped = max(1, min(int(query.limit or 200), 200))
        where: list[str] = []
        params: list[Any] = []
        if not query.include_transient:
            where.append("si.transient = 0")
        normalized_conversation_kind = str(query.conversation_kind or "").strip().lower()
        if normalized_conversation_kind in {"direct", "team"}:
            where.append("si.conversation_kind = ?")
            params.append(normalized_conversation_kind)
        cursor = query.cursor
        if isinstance(cursor, dict) and cursor.get("session_id"):
            cursor_updated_at = _float(cursor.get("updated_at"))
            cursor_started_at = _float(cursor.get("started_at"))
            cursor_session_id = str(cursor.get("session_id") or "")
            where.append(
                "(si.updated_at < ? OR (si.updated_at = ? AND si.started_at < ?) "
                "OR (si.updated_at = ? AND si.started_at = ? AND si.session_id < ?))"
            )
            params.extend([
                cursor_updated_at,
                cursor_updated_at,
                cursor_started_at,
                cursor_updated_at,
                cursor_started_at,
                cursor_session_id,
            ])
        rows = self._conn.execute(
            _session_index_sql(where),
            tuple(params + [capped + 1]),
        ).fetchall()
        has_more = len(rows) > capped
        page = rows[:capped]
        items = [_session_index_row_to_item(row) for row in page]
        next_cursor = items[-1]["_page_cursor"] if has_more and items else None
        return {
            "sessions": items,
            "pageInfo": {"hasMore": has_more, "nextCursor": next_cursor},
        }


def _session_index_sql(where: list[str]) -> str:
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    conversation_session_column = "tmc.conversation_session_id"
    waiting_expr = (
        "(COALESCE(si.waiting_approval, 0) != 0 "
        "OR COALESCE(si.pending_approval_count, 0) > 0 "
        "OR COALESCE(team_pending_approvals.pending_approval_count, 0) > 0 "
        "OR LOWER(COALESCE(am.mission_runtime_status, '')) = 'waiting_approval')"
    )
    terminal_expr = (
        "LOWER(COALESCE(NULLIF(am.mission_runtime_status, ''), NULLIF(si.status, ''), ''))"
    )
    return (
        "WITH active_missions_ranked AS ("
        "    SELECT cm.conversation_id, cm.mission_id, cm.status AS link_status, "
        "           tm.team_id, tm.status AS mission_runtime_status, "
        "           ROW_NUMBER() OVER ("
        "               PARTITION BY cm.conversation_id "
        "               ORDER BY cm.updated_at DESC, cm.added_at DESC, cm.mission_id DESC"
        "           ) AS rn "
        "      FROM conversation_missions cm "
        "      LEFT JOIN team_missions tm ON tm.mission_id = cm.mission_id "
        "     WHERE cm.status = 'active'"
        "), active_missions AS ("
        "    SELECT conversation_id, mission_id, link_status, team_id, mission_runtime_status "
        "      FROM active_missions_ranked "
        "     WHERE rn = 1"
        "), team_pending_approvals AS ("
        "    SELECT tm.conversation_id AS conversation_id, COUNT(*) AS pending_approval_count "
        "      FROM team_mission_nodes n "
        "      JOIN team_missions tm ON tm.mission_id = n.mission_id "
        "     WHERE LOWER(COALESCE(n.kind, '')) = 'approval_gate' "
        "       AND LOWER(COALESCE(n.status, '')) = 'waiting_approval' "
        "       AND LOWER(COALESCE(tm.status, '')) NOT IN "
        "           ('completed','failed','cancelled','canceled','interrupted','draft','idle') "
        "     GROUP BY tm.conversation_id"
        ") "
        "SELECT si.*, "
        "       at.name AS team_name, "
        "       at.avatar_json AS team_avatar_json, "
        "       at.lead_agent_profile_id AS team_lead_profile_id, "
        "       ap.name AS team_lead_profile_name, "
        "       ap.avatar AS team_lead_profile_avatar, "
        "       COALESCE(team_members.team_member_count, 0) AS team_member_count, "
        "       team_members.leader_member_json AS team_leader_member_json, "
        "       team_members.display_members_json AS team_display_members_json, "
        "       tmc.objective AS team_conversation_objective, "
        "       tmc.title AS team_conversation_title, "
        "       tmc.workspace_id AS team_conversation_workspace_id, "
        "       tmc.workspace_path AS team_conversation_workspace_path, "
        "       '' AS team_conversation_active_mission_id, "
        "       COUNT(CASE WHEN act.status IN ('pending','running') THEN 1 END) AS active_activity_count, "
        "       COUNT(CASE WHEN act.status IN ('completed','failed') AND act.read_at IS NULL THEN 1 END) AS unread_completion_count, "
        "       COALESCE(am.mission_id, '') AS active_mission_id, "
        "       am.link_status AS mission_status, "
        "       CASE WHEN COALESCE(am.mission_id, '') != '' THEN 1 ELSE 0 END AS conversation_has_active_mission, "
        "       COALESCE(NULLIF(si.team_id, ''), tmc.team_id, am.team_id, '') AS team_context_team_id, "
        "       COALESCE(NULLIF(si.conversation_id, ''), tmc.conversation_id, am.conversation_id, '') AS team_context_conversation_id, "
        "       COALESCE(NULLIF(si.mission_id, ''), am.mission_id, '') AS team_context_mission_id, "
        "       COALESCE(member_participant.member_id, '') AS team_context_member_id, "
        f"       CASE WHEN {waiting_expr} THEN 0 "
        "            WHEN si.conversation_kind = 'team' THEN "
        "                CASE WHEN COALESCE(am.mission_id, '') != '' "
        "                       AND LOWER(COALESCE(am.mission_runtime_status, '')) NOT IN "
        "                           ('completed','failed','cancelled','canceled','interrupted','draft','idle') "
        "                     THEN 1 ELSE 0 END "
        "            WHEN COALESCE(si.running, 0) != 0 THEN 1 "
        "            ELSE 0 END AS derived_running, "
        f"       CASE WHEN {waiting_expr} THEN 1 ELSE 0 END AS derived_waiting_approval, "
        f"       CASE WHEN {terminal_expr} IN ('completed','failed','cancelled','canceled','interrupted') "
        f"            THEN {terminal_expr} ELSE NULL END AS derived_terminal_status "
        "  FROM session_index si "
        "  LEFT JOIN agent_teams at ON at.id = si.team_id "
        "  LEFT JOIN agent_profiles ap ON ap.id = at.lead_agent_profile_id "
        "  LEFT JOIN ("
        "       SELECT "
        "           ranked.team_id AS team_id, "
        "           COUNT(*) AS team_member_count, "
        "           MAX(CASE WHEN ranked.leader_rank = 1 THEN ranked.member_json END) AS leader_member_json, "
        "           json_group_array(json(ranked.member_json)) "
        "             FILTER (WHERE ranked.display_rank <= 2) AS display_members_json "
        "       FROM ("
        "           SELECT "
        "               m.team_id AS team_id, "
        "               ROW_NUMBER() OVER ("
        "                   PARTITION BY m.team_id "
        "                   ORDER BY "
        "                       CASE WHEN lower(COALESCE(m.role, '')) IN ('lead', 'leader') THEN 0 ELSE 1 END, "
        "                       m.created_at ASC, "
        "                       m.id ASC"
        "               ) AS display_rank, "
        "               ROW_NUMBER() OVER ("
        "                   PARTITION BY m.team_id "
        "                   ORDER BY "
        "                       CASE WHEN lower(COALESCE(m.role, '')) IN ('lead', 'leader') THEN 0 ELSE 1 END, "
        "                       m.created_at ASC, "
        "                       m.id ASC"
        "               ) AS leader_rank, "
        "               json_object("
        "                   'id', m.id, "
        "                   'member_id', m.id, "
        "                   'team_id', m.team_id, "
        "                   'teamId', m.team_id, "
        "                   'agent_profile_id', m.agent_profile_id, "
        "                   'agentProfileId', m.agent_profile_id, "
        "                   'agent_profile_version_id', COALESCE(m.agent_profile_version_id, ''), "
        "                   'agentProfileVersionId', COALESCE(m.agent_profile_version_id, ''), "
        "                   'name', COALESCE(NULLIF(m.profile_name, ''), p.name, ''), "
        "                   'profile_name', COALESCE(NULLIF(m.profile_name, ''), p.name, ''), "
        "                   'profileName', COALESCE(NULLIF(m.profile_name, ''), p.name, ''), "
        "                   'agent_profile_name', COALESCE(NULLIF(m.profile_name, ''), p.name, ''), "
        "                   'agentProfileName', COALESCE(NULLIF(m.profile_name, ''), p.name, ''), "
        "                   'avatar', COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, ''), "
        "                   'profile_avatar', COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, ''), "
        "                   'profileAvatar', COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, ''), "
        "                   'agent_profile_avatar', COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, ''), "
        "                   'agentProfileAvatar', COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, ''), "
        "                   'role', COALESCE(NULLIF(m.role, ''), 'member'), "
        "                   'status', COALESCE(NULLIF(m.status, ''), 'active'), "
        "                   'auto_assignable', COALESCE(m.auto_assignable, 1), "
        "                   'autoAssignable', COALESCE(m.auto_assignable, 1), "
        "                   'max_concurrent_nodes', COALESCE(m.max_concurrent_nodes, 1), "
        "                   'maxConcurrentNodes', COALESCE(m.max_concurrent_nodes, 1), "
        "                   'permission_mode', COALESCE(NULLIF(m.permission_mode, ''), 'inherit_profile'), "
        "                   'permissionMode', COALESCE(NULLIF(m.permission_mode, ''), 'inherit_profile'), "
        "                   'created_at', COALESCE(m.created_at, 0), "
        "                   'updated_at', COALESCE(m.updated_at, 0)"
        "               ) AS member_json "
        "           FROM agent_team_members m "
        "           LEFT JOIN agent_profiles p "
        "             ON p.id = m.agent_profile_id "
        "           WHERE COALESCE(m.status, '') != 'disabled'"
        "       ) ranked "
        "       GROUP BY ranked.team_id"
        "  ) team_members ON team_members.team_id = si.team_id "
        "  LEFT JOIN team_mission_conversations tmc ON tmc.conversation_id = si.conversation_id "
        "  LEFT JOIN active_missions am ON am.conversation_id = si.conversation_id "
        "  LEFT JOIN team_pending_approvals "
        "    ON team_pending_approvals.conversation_id = si.conversation_id "
        "  LEFT JOIN conversation_participants member_participant "
        "    ON member_participant.conversation_session_id = COALESCE(NULLIF("
        + conversation_session_column
        + ", ''), NULLIF(si.conversation_id, ''), si.session_id) "
        "   AND member_participant.runtime_scope_key = si.runtime_scope_key "
        "   AND member_participant.member_id != '' "
        "   AND LOWER(COALESCE(member_participant.role, '')) = 'member' "
        "  LEFT JOIN activities act "
        "    ON act.conversation_id = COALESCE(NULLIF(si.conversation_id, ''), si.session_id)"
        + where_sql
        + " GROUP BY si.session_id "
        " ORDER BY si.updated_at DESC, si.started_at DESC, si.session_id DESC LIMIT ?"
    )


def _session_index_row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    for flag in (
        "transient",
        "running",
        "waiting_approval",
        "derived_running",
        "derived_waiting_approval",
    ):
        if flag in item:
            item[flag] = bool(item.get(flag))
    for count_field in ("active_activity_count", "unread_completion_count"):
        item[count_field] = int(item.get(count_field) or 0)
    if (
        item.get("conversation_kind") == "team"
        and item.get("conversation_id")
        and "conversation_has_active_mission" in item
    ):
        item["running"] = bool(item.get("conversation_has_active_mission"))
    item.pop("conversation_has_active_mission", None)
    team_context = {
        "team_id": str(item.get("team_context_team_id") or ""),
        "team_conversation_id": str(item.get("team_context_conversation_id") or ""),
        "mission_id": str(item.get("team_context_mission_id") or ""),
        "member_id": str(item.get("team_context_member_id") or ""),
    }
    item["team_context"] = (
        team_context
        if any(team_context.values()) or item.get("conversation_kind") == "team"
        else None
    )
    item["derived_state"] = {
        "running": bool(item.get("derived_running")),
        "waiting_approval": bool(item.get("derived_waiting_approval")),
        "terminal_status": item.get("derived_terminal_status") or None,
    }
    item["_page_cursor"] = {
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "session_id": row["session_id"],
    }
    return item


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["SessionIndexQuery", "SessionIndexReadModel"]
