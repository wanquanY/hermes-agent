from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any, Dict, List


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _timestamp(value: Any = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0
    return parsed if parsed > 0 else time.time()


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _text(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


class TeamRegistryRepo:
    """Canonical Hermes-owned team registry.

    The registry stores product team configuration used by Team Mission.
    Runtime conversations, mission graphs, run bindings, artifacts, and memory
    remain in the Team Mission tables; this registry only owns reusable team
    and member definitions.
    """

    def __init__(self, conn: sqlite3.Connection, execute_write: Callable[[Any], Any], lock: Any) -> None:
        self._conn = conn
        self._execute_write = execute_write
        self._lock = lock

    def _agent_team_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        if row is None:
            return {}
        return {
            "id": _text(_row_value(row, "id", "")),
            "team_id": _text(_row_value(row, "id", "")),
            "name": _text(_row_value(row, "name", "")),
            "avatar": _json_loads(_row_value(row, "avatar_json", ""), None),
            "description": _text(_row_value(row, "description", "")),
            "source_kind": _text(_row_value(row, "source_kind", "")),
            "metadata": _json_loads(_row_value(row, "metadata_json", ""), {}),
            "lead_agent_profile_id": _text(_row_value(row, "lead_agent_profile_id", "")),
            "default_mode": _text(_row_value(row, "default_mode", "")) or "supervised_mission",
            "policy": _json_loads(_row_value(row, "policy_json", ""), {}),
            "status": _text(_row_value(row, "status", "")) or "active",
            "created_at": float(_row_value(row, "created_at", 0) or 0),
            "updated_at": float(_row_value(row, "updated_at", 0) or 0),
        }

    def _agent_team_member_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        if row is None:
            return {}
        member = {
            "id": _text(_row_value(row, "id", "")),
            "member_id": _text(_row_value(row, "id", "")),
            "team_id": _text(_row_value(row, "team_id", "")),
            "agent_profile_id": _text(_row_value(row, "agent_profile_id", "")),
            "agent_profile_version_id": _text(_row_value(row, "agent_profile_version_id", "")),
            "role": _text(_row_value(row, "role", "")) or "member",
            "capability_tags": _json_loads(_row_value(row, "capability_tags_json", ""), []),
            "auto_assignable": bool(int(_row_value(row, "auto_assignable", 1) or 0)),
            "max_concurrent_nodes": max(1, int(_row_value(row, "max_concurrent_nodes", 1) or 1)),
            "permission_mode": _text(_row_value(row, "permission_mode", "")) or "inherit_profile",
            "status": _text(_row_value(row, "status", "")) or "active",
            "created_at": float(_row_value(row, "created_at", 0) or 0),
            "updated_at": float(_row_value(row, "updated_at", 0) or 0),
        }
        profile_name = _text(
            _row_value(row, "display_profile_name", "")
            or _row_value(row, "profile_name", "")
        )
        profile_avatar = _text(
            _row_value(row, "display_profile_avatar", "")
            or _row_value(row, "profile_avatar", "")
        )
        if profile_name:
            member.update({
                "name": profile_name,
                "profile_name": profile_name,
                "profileName": profile_name,
                "agent_profile_name": profile_name,
                "agentProfileName": profile_name,
            })
        if profile_avatar:
            member.update({
                "avatar": profile_avatar,
                "profile_avatar": profile_avatar,
                "profileAvatar": profile_avatar,
                "agent_profile_avatar": profile_avatar,
                "agentProfileAvatar": profile_avatar,
            })
        return member

    def _agent_team_summary_from_row(
        self,
        row: sqlite3.Row | None,
        *,
        display_members: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        team = self._agent_team_from_row(row)
        if not team:
            return {}
        member_count = int(_row_value(row, "member_count", 0) or 0)
        leader = {}
        leader_member_id = _text(_row_value(row, "leader_member_id", ""))
        if leader_member_id:
            leader = {
                "id": leader_member_id,
                "member_id": leader_member_id,
                "team_id": _text(_row_value(row, "leader_team_id", "")),
                "agent_profile_id": _text(_row_value(row, "leader_agent_profile_id", "")),
                "agent_profile_version_id": _text(_row_value(row, "leader_agent_profile_version_id", "")),
                "role": _text(_row_value(row, "leader_role", "")) or "member",
                "status": _text(_row_value(row, "leader_status", "")) or "active",
            }
            leader_profile_name = _text(_row_value(row, "leader_profile_name", ""))
            leader_profile_avatar = _text(_row_value(row, "leader_profile_avatar", ""))
            if leader_profile_name:
                leader.update({
                    "name": leader_profile_name,
                    "profile_name": leader_profile_name,
                    "profileName": leader_profile_name,
                    "agent_profile_name": leader_profile_name,
                    "agentProfileName": leader_profile_name,
                })
            if leader_profile_avatar:
                leader.update({
                    "avatar": leader_profile_avatar,
                    "profile_avatar": leader_profile_avatar,
                    "profileAvatar": leader_profile_avatar,
                    "agent_profile_avatar": leader_profile_avatar,
                    "agentProfileAvatar": leader_profile_avatar,
                })
        visible_display_members = list(display_members or [])
        if not visible_display_members and leader:
            visible_display_members = [leader]
        return {
            **team,
            "member_count": member_count,
            "memberCount": member_count,
            "leader_member": leader,
            "leaderMember": leader,
            "display_members": visible_display_members,
            "displayMembers": visible_display_members,
        }

    def upsert_agent_team(
        self,
        *,
        team_id: str,
        name: str,
        avatar: Any = None,
        description: str = "",
        source_kind: str = "",
        metadata: Dict[str, Any] | None = None,
        lead_agent_profile_id: str = "",
        default_mode: str = "supervised_mission",
        policy: Dict[str, Any] | None = None,
        status: str = "active",
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        resolved_team_id = _text(team_id)
        if not resolved_team_id:
            raise ValueError("team_id required")
        resolved_name = _text(name)
        if not resolved_name:
            raise ValueError("team name required")
        now = time.time()
        created = _timestamp(created_at or now)
        updated = _timestamp(updated_at or now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT created_at, status FROM agent_teams WHERE id = ?",
                (resolved_team_id,),
            ).fetchone()
            existing_status = _text(_row_value(existing, "status", ""))
            next_status = _text(status) or "active"
            if existing_status == "archived" and next_status != "archived":
                raise ValueError(f"team archived: {resolved_team_id}")
            conn.execute(
                """
                INSERT INTO agent_teams (
                    id, name, avatar_json, description, source_kind, metadata_json, lead_agent_profile_id,
                    default_mode, policy_json, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    avatar_json = excluded.avatar_json,
                    description = excluded.description,
                    source_kind = excluded.source_kind,
                    metadata_json = excluded.metadata_json,
                    lead_agent_profile_id = excluded.lead_agent_profile_id,
                    default_mode = excluded.default_mode,
                    policy_json = excluded.policy_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    resolved_team_id,
                    resolved_name,
                    _json_dumps(avatar) if avatar is not None else "",
                    _text(description),
                    _text(source_kind),
                    _json_dumps(metadata or {}),
                    _text(lead_agent_profile_id),
                    _text(default_mode) or "supervised_mission",
                    _json_dumps(policy or {}),
                    next_status,
                    float(_row_value(existing, "created_at", created) or created),
                    updated,
                ),
            )
            return self._agent_team_from_row(conn.execute(
                "SELECT * FROM agent_teams WHERE id = ?",
                (resolved_team_id,),
            ).fetchone())

        return self._execute_write(_do)

    def get_agent_team(self, team_id: str) -> Dict[str, Any]:
        normalized = _text(team_id)
        if not normalized:
            return {}
        with self._lock:
            return self._agent_team_from_row(self._conn.execute(
                "SELECT * FROM agent_teams WHERE id = ?",
                (normalized,),
            ).fetchone())

    def list_agent_teams(self, *, include_archived: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if include_archived:
                rows = self._conn.execute(
                    "SELECT * FROM agent_teams ORDER BY updated_at DESC, id ASC",
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM agent_teams
                    WHERE status != 'archived'
                    ORDER BY updated_at DESC, id ASC
                    """,
                ).fetchall()
        return [team for team in (self._agent_team_from_row(row) for row in rows) if team]

    def list_agent_team_summaries(self, *, include_archived: bool = False) -> List[Dict[str, Any]]:
        where_clause = "" if include_archived else "WHERE t.status != 'archived'"
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    t.*,
                    COALESCE(member_counts.member_count, 0) AS member_count,
                    leader.id AS leader_member_id,
                    leader.team_id AS leader_team_id,
                    leader.agent_profile_id AS leader_agent_profile_id,
                    leader.agent_profile_version_id AS leader_agent_profile_version_id,
                    leader.role AS leader_role,
                    leader.status AS leader_status,
                    COALESCE(NULLIF(leader.profile_name, ''), leader_profile.name, '') AS leader_profile_name,
                    COALESCE(NULLIF(leader.profile_avatar, ''), leader_profile.avatar, '') AS leader_profile_avatar
                FROM agent_teams t
                LEFT JOIN (
                    SELECT team_id, COUNT(*) AS member_count
                    FROM agent_team_members
                    GROUP BY team_id
                ) member_counts ON member_counts.team_id = t.id
                LEFT JOIN agent_team_members leader ON leader.id = (
                    SELECT lm.id
                    FROM agent_team_members lm
                    WHERE lm.team_id = t.id
                    ORDER BY
                        CASE WHEN lower(lm.role) IN ('lead', 'leader') THEN 0 ELSE 1 END,
                        lm.created_at ASC,
                        lm.id ASC
                    LIMIT 1
                )
                LEFT JOIN agent_profiles leader_profile
                  ON leader_profile.id = leader.agent_profile_id
                {where_clause}
                ORDER BY t.updated_at DESC, t.id ASC
                """,
            ).fetchall()
            team_ids = [_text(_row_value(row, "id", "")) for row in rows if _text(_row_value(row, "id", ""))]
            display_members_by_team: Dict[str, List[Dict[str, Any]]] = {}
            if team_ids:
                placeholders = ",".join("?" for _ in team_ids)
                display_rows = self._conn.execute(
                    f"""
                    WITH ranked_members AS (
                        SELECT
                            m.*,
                            COALESCE(NULLIF(m.profile_name, ''), p.name, '') AS display_profile_name,
                            COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, '') AS display_profile_avatar,
                            ROW_NUMBER() OVER (
                                PARTITION BY m.team_id
                                ORDER BY
                                    CASE WHEN lower(m.role) IN ('lead', 'leader') THEN 0 ELSE 1 END,
                                    m.created_at ASC,
                                    m.id ASC
                            ) AS display_rank
                        FROM agent_team_members m
                        LEFT JOIN agent_profiles p
                          ON p.id = m.agent_profile_id
                        WHERE m.team_id IN ({placeholders})
                          AND m.status != 'disabled'
                    )
                    SELECT *
                    FROM ranked_members
                    WHERE display_rank <= 2
                    ORDER BY team_id ASC, display_rank ASC
                    """,
                    tuple(team_ids),
                ).fetchall()
                for display_row in display_rows:
                    member = self._agent_team_member_from_row(display_row)
                    team_id = _text(member.get("team_id"))
                    if not team_id:
                        continue
                    display_members_by_team.setdefault(team_id, []).append(member)
        return [
            team
            for team in (
                self._agent_team_summary_from_row(
                    row,
                    display_members=display_members_by_team.get(_text(_row_value(row, "id", "")), []),
                )
                for row in rows
            )
            if team
        ]

    def archive_agent_team(self, team_id: str) -> Dict[str, Any]:
        team = self.get_agent_team(team_id)
        if not team:
            return {}
        return self.upsert_agent_team(
            team_id=team["id"],
            name=team["name"],
            avatar=team.get("avatar"),
            description=team.get("description", ""),
            lead_agent_profile_id=team.get("lead_agent_profile_id", ""),
            default_mode=team.get("default_mode", ""),
            policy=team.get("policy") if isinstance(team.get("policy"), dict) else {},
            status="archived",
            created_at=team.get("created_at"),
        )

    def upsert_agent_team_member(
        self,
        *,
        member_id: str,
        team_id: str,
        agent_profile_id: str,
        agent_profile_version_id: str = "",
        role: str = "member",
        capability_tags: List[str] | None = None,
        auto_assignable: bool = True,
        max_concurrent_nodes: int = 1,
        permission_mode: str = "inherit_profile",
        status: str = "active",
        profile_name: str = "",
        profile_avatar: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        resolved_member_id = _text(member_id)
        resolved_team_id = _text(team_id)
        resolved_profile_id = _text(agent_profile_id)
        if not resolved_member_id:
            raise ValueError("member_id required")
        if not resolved_team_id:
            raise ValueError("team_id required")
        if not resolved_profile_id:
            raise ValueError("agent_profile_id required")
        now = time.time()
        created = _timestamp(created_at or now)
        updated = _timestamp(updated_at or now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            team = conn.execute(
                "SELECT status FROM agent_teams WHERE id = ?",
                (resolved_team_id,),
            ).fetchone()
            if _text(_row_value(team, "status", "")) == "archived":
                raise ValueError(f"team archived: {resolved_team_id}")
            existing = conn.execute(
                """
                SELECT id, created_at FROM agent_team_members
                WHERE id = ? OR (team_id = ? AND agent_profile_id = ?)
                ORDER BY id = ? DESC
                LIMIT 1
                """,
                (
                    resolved_member_id,
                    resolved_team_id,
                    resolved_profile_id,
                    resolved_member_id,
                ),
            ).fetchone()
            values = (
                resolved_member_id,
                resolved_team_id,
                resolved_profile_id,
                _text(agent_profile_version_id),
                _text(profile_name),
                _text(profile_avatar),
                _text(role) or "member",
                _json_dumps(_string_list(capability_tags or [])),
                1 if auto_assignable is not False else 0,
                max(1, int(max_concurrent_nodes or 1)),
                _text(permission_mode) or "inherit_profile",
                _text(status) or "active",
                float(_row_value(existing, "created_at", created) or created),
                updated,
            )
            if existing:
                conn.execute(
                    """
                    UPDATE agent_team_members
                    SET id = ?,
                        team_id = ?,
                        agent_profile_id = ?,
                        agent_profile_version_id = ?,
                        profile_name = ?,
                        profile_avatar = ?,
                        role = ?,
                        capability_tags_json = ?,
                        auto_assignable = ?,
                        max_concurrent_nodes = ?,
                        permission_mode = ?,
                        status = ?,
                        created_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (*values, _row_value(existing, "id", resolved_member_id)),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO agent_team_members (
                        id, team_id, agent_profile_id, agent_profile_version_id,
                        profile_name, profile_avatar,
                        role, capability_tags_json, auto_assignable,
                        max_concurrent_nodes, permission_mode, status,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            return self._agent_team_member_from_row(conn.execute(
                """
                SELECT
                    m.*,
                    COALESCE(NULLIF(m.profile_name, ''), p.name, '') AS display_profile_name,
                    COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, '') AS display_profile_avatar
                FROM agent_team_members m
                LEFT JOIN agent_profiles p
                  ON p.id = m.agent_profile_id
                WHERE m.id = ?
                """,
                (resolved_member_id,),
            ).fetchone())

        return self._execute_write(_do)

    def get_agent_team_member(self, member_id: str) -> Dict[str, Any]:
        normalized = _text(member_id)
        if not normalized:
            return {}
        with self._lock:
            return self._agent_team_member_from_row(self._conn.execute(
                """
                SELECT
                    m.*,
                    COALESCE(NULLIF(m.profile_name, ''), p.name, '') AS display_profile_name,
                    COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, '') AS display_profile_avatar
                FROM agent_team_members m
                LEFT JOIN agent_profiles p
                  ON p.id = m.agent_profile_id
                WHERE m.id = ?
                """,
                (normalized,),
            ).fetchone())

    def list_agent_team_members(self, team_id: str) -> List[Dict[str, Any]]:
        normalized = _text(team_id)
        if not normalized:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    m.*,
                    COALESCE(NULLIF(m.profile_name, ''), p.name, '') AS display_profile_name,
                    COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, '') AS display_profile_avatar
                FROM agent_team_members m
                LEFT JOIN agent_profiles p
                  ON p.id = m.agent_profile_id
                WHERE m.team_id = ?
                ORDER BY m.role = 'lead' DESC, m.created_at ASC, m.id ASC
                """,
                (normalized,),
            ).fetchall()
        return [member for member in (self._agent_team_member_from_row(row) for row in rows) if member]

    def delete_agent_team_member(self, member_id: str) -> Dict[str, Any]:
        existing = self.get_agent_team_member(member_id)
        if not existing:
            return {}
        team = self.get_agent_team(existing.get("team_id", ""))
        if _text(team.get("status")) == "archived":
            raise ValueError(f"team archived: {team.get('id') or existing.get('team_id')}")

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute("DELETE FROM agent_team_members WHERE id = ?", (_text(member_id),))
            return existing

        return self._execute_write(_do)

    def get_agent_team_with_members(self, team_id: str) -> Dict[str, Any]:
        team = self.get_agent_team(team_id)
        if not team:
            return {}
        return {
            **team,
            "members": self.list_agent_team_members(team["id"]),
        }


def ensure_team_registry_repository_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the TeamRegistryRepo-owned table family."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            avatar_json TEXT,
            description TEXT,
            source_kind TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            lead_agent_profile_id TEXT,
            default_mode TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_team_members (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL REFERENCES agent_teams(id) ON DELETE CASCADE,
            agent_profile_id TEXT NOT NULL,
            agent_profile_version_id TEXT,
            profile_name TEXT,
            profile_avatar TEXT,
            role TEXT NOT NULL,
            capability_tags_json TEXT NOT NULL,
            auto_assignable INTEGER NOT NULL,
            max_concurrent_nodes INTEGER NOT NULL,
            permission_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(team_id, agent_profile_id)
        );

        CREATE INDEX IF NOT EXISTS idx_agent_teams_status_updated
            ON agent_teams(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_team_members_team_id
            ON agent_team_members(team_id, role, created_at ASC);
        """
    )
    _ensure_columns(
        conn,
        "agent_teams",
        {
            "avatar_json": "TEXT",
            "description": "TEXT",
            "source_kind": "TEXT",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "lead_agent_profile_id": "TEXT",
            "default_mode": "TEXT NOT NULL DEFAULT 'supervised_mission'",
            "policy_json": "TEXT NOT NULL DEFAULT '{}'",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "created_at": "REAL NOT NULL DEFAULT 0",
            "updated_at": "REAL NOT NULL DEFAULT 0",
        },
    )
    _ensure_columns(
        conn,
        "agent_team_members",
        {
            "agent_profile_version_id": "TEXT",
            "profile_name": "TEXT",
            "profile_avatar": "TEXT",
            "capability_tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "auto_assignable": "INTEGER NOT NULL DEFAULT 1",
            "max_concurrent_nodes": "INTEGER NOT NULL DEFAULT 1",
            "permission_mode": "TEXT NOT NULL DEFAULT 'inherit_profile'",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "created_at": "REAL NOT NULL DEFAULT 0",
            "updated_at": "REAL NOT NULL DEFAULT 0",
        },
    )


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = _table_columns(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}
