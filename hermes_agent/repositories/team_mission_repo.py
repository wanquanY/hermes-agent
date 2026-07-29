"""TeamMissionRepo protocol + concrete impl (spec §4.4).

v3.0.2 P0-B4 decision: this repository owns ``activities`` +
``activity_commands`` — a separate ActivityRepo is not warranted because
all four activity kinds are team/mission adjacent, and the frontend items
SSoT requires ``activity_seq`` to share the canonical seq domain with
``run_events`` (spec §6.5).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from hermes_agent.domain.seq_allocator import allocate_only
from hermes_agent.repositories.base import RepositoryConnection
from hermes_team_mission.domain.activity import is_legal_transition


ActivityKind = Literal[
    "async_agent_dispatch",
    "async_team_dispatch",
    "team_mission_activity",
    "dispatch_completion",
]


NodeStatus = Literal[
    "pending",
    "planning",
    "planning_clarify",
    "waiting_approval",
    "running",
    "completed",
    "failed",
    "cancelled",
]


ActivityStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class MissionSpec:
    mission_id: str
    session_id: str
    title: str = ""
    plan_json: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeSpec:
    mission_id: str
    node_id: str
    kind: str = "task"
    title: str = ""
    plan_json: str = ""


@dataclass(frozen=True)
class EdgeSpec:
    mission_id: str
    from_node_id: str
    to_node_id: str
    kind: str = "sequence"


@dataclass(frozen=True)
class Mission:
    mission_id: str
    session_id: str
    title: str
    status: str
    created_at: float
    updated_at: float
    plan_json: str = ""


@dataclass(frozen=True)
class MissionNode:
    mission_id: str
    node_id: str
    kind: str
    title: str
    status: str
    bound_run_id: str = ""
    plan_json: str = ""
    updated_at: float = 0.0


@dataclass(frozen=True)
class MissionEdge:
    mission_id: str
    from_node_id: str
    to_node_id: str
    kind: str = "sequence"


@dataclass(frozen=True)
class MissionGraph:
    mission: Mission
    nodes: tuple[MissionNode, ...]
    edges: tuple[MissionEdge, ...]


@dataclass(frozen=True)
class ActivitySpec:
    activity_id: str
    session_id: str
    kind: ActivityKind
    target_id: str = ""
    prompt_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass(frozen=True)
class Activity:
    activity_id: str
    session_id: str
    kind: ActivityKind
    activity_seq: int
    status: ActivityStatus
    target_id: str = ""
    prompt_summary: str = ""
    result_summary: str = ""
    started_at: float = 0.0
    completed_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunConversationBinding:
    conversation_session_id: str
    conversation_scope_key: str
    mission_is_terminal: bool = False


_ACTIVITY_KINDS = frozenset({
    "async_agent_dispatch",
    "async_team_dispatch",
    "team_mission_activity",
    "dispatch_completion",
})

_LEGACY_ACTIVITY_KINDS = frozenset({
    "chat",
    "agent_dispatch",
    "team_dispatch",
    "member_chat",
    "mission",
})
_TERMINAL_ACTIVITY_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
_LEGACY_ACTIVITY_STATUS_ALLOWED_PREVIOUS = {
    "running": ("pending",),
    "completed": ("pending", "running"),
    "failed": ("pending", "running"),
    "cancelled": ("pending", "running"),
    "interrupted": ("pending", "running"),
}


@runtime_checkable
class TeamMissionRepo(Protocol):
    def create_mission(self, session_id: str, spec: MissionSpec) -> Mission: ...

    def get_graph(self, mission_id: str) -> MissionGraph | None: ...

    def get_node(self, mission_id: str, node_id: str) -> MissionNode | None: ...

    def bind_run(self, mission_id: str, node_id: str, run_id: str) -> None: ...

    def get_run_conversation_binding(self, run_id: str) -> RunConversationBinding | None: ...

    def update_node_status(
        self,
        mission_id: str,
        node_id: str,
        status: NodeStatus,
    ) -> None: ...

    def append_activity(self, session_id: str, spec: ActivitySpec) -> Activity: ...

    def list_activities(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        kinds: set[ActivityKind] | None = None,
        limit: int = 200,
    ) -> list[Activity]: ...

    def update_activity_status(self, activity_id: str, status: ActivityStatus) -> Activity: ...

    def insert_activity_command(
        self,
        *,
        command_id: str,
        activity_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_activity_command(self, command_id: str) -> dict[str, Any]: ...

    def list_pending_activity_commands(
        self,
        *,
        states: tuple[str, ...] = ("accepted", "dispatched"),
        limit: int = 500,
    ) -> list[dict[str, Any]]: ...

    def update_activity_command_state(
        self,
        command_id: str,
        *,
        next_state: str,
        error_reason: str = "",
        result_event_id: int | None = None,
    ) -> dict[str, Any]: ...

    def list_activity_commands_for_activity(
        self,
        activity_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    def migrate_legacy_activities_kind_mission_check(self) -> bool: ...


class TeamMissionRepoImpl:
    """SQLite-backed TeamMissionRepo (spec §4.4 + §6.5).

    Owned tables (v3.0.2):
    - team_missions
    - team_mission_nodes
    - team_mission_edges
    - team_mission_run_bindings
    - v3_activities  (spec-native 4-kind vocabulary, avoids the legacy
                     ``activities`` table's CHECK constraint on
                     ``kind IN ('chat', 'agent_dispatch', ...)``)
    - v3_activity_commands (Phase D4-2, not yet exercised)
    """

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def rebase_workspace_paths(self, old_path: str, new_path: str) -> dict[str, int]:
        source = str(old_path or "").strip()
        target = str(new_path or "").strip()
        if not source or not target or source == target:
            return {
                "team_missions": 0,
                "team_mission_conversations": 0,
            }
        mission_rows = self._conn.execute(
            "UPDATE team_missions SET workspace_path = ? WHERE workspace_path = ?",
            (target, source),
        ).rowcount
        conversation_rows = self._conn.execute(
            """
            UPDATE team_mission_conversations
               SET workspace_path = ?
             WHERE workspace_path = ?
            """,
            (target, source),
        ).rowcount
        return {
            "team_missions": int(mission_rows or 0),
            "team_mission_conversations": int(conversation_rows or 0),
        }

    # ------------------------------------------------------------------
    # Mission graph
    # ------------------------------------------------------------------

    def create_mission(self, session_id: str, spec: MissionSpec) -> Mission:
        stable_sid = str(session_id or "").strip()
        stable_mission = str(spec.mission_id or "").strip()
        if not stable_sid or not stable_mission:
            raise ValueError("session_id and MissionSpec.mission_id are required")
        now = time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO team_missions (
                mission_id, session_id, title, status, created_at, updated_at,
                plan_json, metadata_json
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                stable_mission,
                stable_sid,
                str(spec.title or ""),
                now,
                now,
                str(spec.plan_json or ""),
                json.dumps(spec.metadata or {}, ensure_ascii=False),
            ),
        )
        return Mission(
            mission_id=stable_mission,
            session_id=stable_sid,
            title=str(spec.title or ""),
            status="pending",
            created_at=now,
            updated_at=now,
            plan_json=str(spec.plan_json or ""),
        )

    def get_graph(self, mission_id: str) -> MissionGraph | None:
        stable = str(mission_id or "").strip()
        if not stable:
            return None
        mission_row = self._conn.execute(
            """
            SELECT mission_id, session_id, title, status, created_at,
                   updated_at, plan_json
              FROM team_missions
             WHERE mission_id = ?
            """,
            (stable,),
        ).fetchone()
        if mission_row is None:
            return None
        mission = _row_to_mission(mission_row)
        node_rows = self._conn.execute(
            """
            SELECT mission_id, node_id, kind, title, status, bound_run_id,
                   plan_json, updated_at
              FROM team_mission_nodes
             WHERE mission_id = ?
             ORDER BY node_id ASC
            """,
            (stable,),
        ).fetchall()
        edge_rows = self._conn.execute(
            """
            SELECT mission_id, from_node_id, to_node_id, kind
              FROM team_mission_edges
             WHERE mission_id = ?
             ORDER BY from_node_id, to_node_id
            """,
            (stable,),
        ).fetchall()
        return MissionGraph(
            mission=mission,
            nodes=tuple(_row_to_node(r) for r in node_rows),
            edges=tuple(_row_to_edge(r) for r in edge_rows),
        )

    def add_node(self, spec: NodeSpec) -> MissionNode:
        stable_mission = str(spec.mission_id or "").strip()
        stable_node = str(spec.node_id or "").strip()
        if not stable_mission or not stable_node:
            raise ValueError("mission_id and NodeSpec.node_id are required")
        now = time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO team_mission_nodes (
                mission_id, node_id, kind, title, status, bound_run_id,
                plan_json, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', '', ?, ?)
            """,
            (
                stable_mission,
                stable_node,
                str(spec.kind or "task"),
                str(spec.title or ""),
                str(spec.plan_json or ""),
                now,
            ),
        )
        got = self.get_node(stable_mission, stable_node)
        assert got is not None
        return got

    def add_edge(self, spec: EdgeSpec) -> MissionEdge:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO team_mission_edges (
                mission_id, from_node_id, to_node_id, kind
            ) VALUES (?, ?, ?, ?)
            """,
            (
                str(spec.mission_id or "").strip(),
                str(spec.from_node_id or "").strip(),
                str(spec.to_node_id or "").strip(),
                str(spec.kind or "sequence"),
            ),
        )
        return MissionEdge(
            mission_id=str(spec.mission_id or "").strip(),
            from_node_id=str(spec.from_node_id or "").strip(),
            to_node_id=str(spec.to_node_id or "").strip(),
            kind=str(spec.kind or "sequence"),
        )

    def get_node(self, mission_id: str, node_id: str) -> MissionNode | None:
        row = self._conn.execute(
            """
            SELECT mission_id, node_id, kind, title, status, bound_run_id,
                   plan_json, updated_at
              FROM team_mission_nodes
             WHERE mission_id = ? AND node_id = ?
            """,
            (str(mission_id or ""), str(node_id or "")),
        ).fetchone()
        if row is None:
            return None
        return _row_to_node(row)

    def bind_run(self, mission_id: str, node_id: str, run_id: str) -> None:
        stable_mission = str(mission_id or "").strip()
        stable_node = str(node_id or "").strip()
        stable_run = str(run_id or "").strip()
        if not stable_mission or not stable_node or not stable_run:
            raise ValueError("mission_id, node_id, run_id all required")
        now = time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO team_mission_run_bindings (
                mission_id, node_id, run_id, bound_at
            ) VALUES (?, ?, ?, ?)
            """,
            (stable_mission, stable_node, stable_run, now),
        )
        self._conn.execute(
            """
            UPDATE team_mission_nodes
               SET bound_run_id = ?, updated_at = ?
             WHERE mission_id = ? AND node_id = ?
            """,
            (stable_run, now, stable_mission, stable_node),
        )

    def get_run_conversation_binding(self, run_id: str) -> RunConversationBinding | None:
        stable_run = str(run_id or "").strip()
        if not stable_run:
            return None
        row = self._conn.execute(
            """
            SELECT tmc.conversation_session_id, tmc.conversation_id, tm.status
              FROM team_mission_run_bindings tmrb
              JOIN team_missions tm
                ON tm.mission_id = tmrb.mission_id
              JOIN team_mission_conversations tmc
                ON tmc.conversation_id = tm.conversation_id
             WHERE tmrb.run_id = ?
            """,
            (stable_run,),
        ).fetchone()
        if row is None:
            return None
        conversation_session_id = str(row[0] or "").strip()
        conversation_id = str(row[1] or "").strip()
        mission_status = str(row[2] or "").strip().lower()
        if not conversation_session_id:
            return None
        return RunConversationBinding(
            conversation_session_id=conversation_session_id,
            conversation_scope_key=f"team:{conversation_id}:leader-conversation" if conversation_id else "",
            mission_is_terminal=mission_status in {"completed", "failed", "cancelled", "canceled", "interrupted"},
        )

    def update_node_status(
        self,
        mission_id: str,
        node_id: str,
        status: NodeStatus,
    ) -> None:
        self._conn.execute(
            """
            UPDATE team_mission_nodes
               SET status = ?, updated_at = ?
             WHERE mission_id = ? AND node_id = ?
            """,
            (
                str(status or "pending"),
                time.time(),
                str(mission_id or ""),
                str(node_id or ""),
            ),
        )

    # ------------------------------------------------------------------
    # Activities — spec §4.4 P0-B4
    # ------------------------------------------------------------------

    def append_activity(self, session_id: str, spec: ActivitySpec) -> Activity:
        stable_sid = str(session_id or "").strip()
        stable_aid = str(spec.activity_id or "").strip()
        if not stable_sid or not stable_aid:
            raise ValueError("session_id and ActivitySpec.activity_id are required")
        if spec.kind not in _ACTIVITY_KINDS:
            raise ValueError(
                f"ActivitySpec.kind must be one of {_ACTIVITY_KINDS}, got {spec.kind!r}"
            )
        now = float(spec.timestamp or time.time())
        # Share the canonical seq domain with run_events (spec §6.5).
        activity_seq = allocate_only(self._conn, session_id=stable_sid, updated_at=now)
        metadata_json = json.dumps(spec.metadata or {}, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO v3_activities (
                activity_id, session_id, kind, activity_seq, status,
                target_id, prompt_summary, result_summary, started_at,
                completed_at, metadata_json
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, '', ?, NULL, ?)
            """,
            (
                stable_aid,
                stable_sid,
                str(spec.kind),
                int(activity_seq),
                str(spec.target_id or ""),
                str(spec.prompt_summary or ""),
                now,
                metadata_json,
            ),
        )
        return Activity(
            activity_id=stable_aid,
            session_id=stable_sid,
            kind=spec.kind,
            activity_seq=int(activity_seq),
            status="pending",
            target_id=str(spec.target_id or ""),
            prompt_summary=str(spec.prompt_summary or ""),
            started_at=now,
            completed_at=None,
            metadata=dict(spec.metadata or {}),
        )

    def list_activities(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        kinds: set[ActivityKind] | None = None,
        limit: int = 200,
    ) -> list[Activity]:
        stable_sid = str(session_id or "").strip()
        if not stable_sid:
            return []
        clauses = ["session_id = ?"]
        params: list[Any] = [stable_sid]
        if after_seq:
            clauses.append("activity_seq > ?")
            params.append(int(after_seq))
        if kinds:
            invalid = kinds - _ACTIVITY_KINDS
            if invalid:
                raise ValueError(f"unknown activity kinds: {invalid}")
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(sorted(kinds))
        sql = (
            "SELECT activity_id, session_id, kind, activity_seq, status, "
            "target_id, prompt_summary, result_summary, started_at, "
            "completed_at, metadata_json "
            "FROM v3_activities "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY activity_seq ASC "
            "LIMIT ?"
        )
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_activity(r) for r in rows]

    def update_activity_status(self, activity_id: str, status: ActivityStatus) -> Activity:
        stable_aid = str(activity_id or "").strip()
        if not stable_aid:
            raise ValueError("activity_id is required")
        now = time.time()
        completed_at = now if status in {"completed", "failed", "cancelled"} else None
        self._conn.execute(
            """
            UPDATE v3_activities
               SET status = ?,
                   completed_at = COALESCE(?, completed_at)
             WHERE activity_id = ?
            """,
            (str(status), completed_at, stable_aid),
        )
        row = self._conn.execute(
            """
            SELECT activity_id, session_id, kind, activity_seq, status,
                   target_id, prompt_summary, result_summary, started_at,
                   completed_at, metadata_json
              FROM v3_activities
             WHERE activity_id = ?
            """,
            (stable_aid,),
        ).fetchone()
        if row is None:
            raise LookupError(f"activity {stable_aid!r} not found")
        return _row_to_activity(row)

    def insert_activity_command(
        self,
        *,
        command_id: str,
        activity_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stable_command = _text(command_id)
        stable_activity = _text(activity_id)
        stable_kind = _text(kind)
        if not stable_command or not stable_activity:
            return {}
        now = time.time()
        cursor = self._conn.execute(
            """
            INSERT INTO activity_commands (
                command_id, activity_id, kind, payload_json, intent_at,
                state, state_changed_at, result_event_id, error_reason,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, 'accepted', ?, NULL, '', ?)
            ON CONFLICT(command_id) DO NOTHING
            """,
            (
                stable_command,
                stable_activity,
                stable_kind,
                _activity_command_json(payload),
                now,
                now,
                _activity_command_json(metadata),
            ),
        )
        if int(cursor.rowcount or 0) == 0:
            return {}
        return self.get_activity_command(stable_command)

    def get_activity_command(self, command_id: str) -> dict[str, Any]:
        stable_command = _text(command_id)
        if not stable_command:
            return {}
        row = self._conn.execute(
            "SELECT * FROM activity_commands WHERE command_id = ?",
            (stable_command,),
        ).fetchone()
        return _activity_command_row_to_dict(row)

    def list_pending_activity_commands(
        self,
        *,
        states: tuple[str, ...] = ("accepted", "dispatched"),
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        normalized_states = tuple(state for state in (_text(value) for value in states or ()) if state)
        if not normalized_states:
            return []
        bounded_limit = _activity_command_limit(limit, 500)
        placeholders = ", ".join("?" for _ in normalized_states)
        rows = self._conn.execute(
            f"""
            SELECT *
              FROM activity_commands
             WHERE state IN ({placeholders})
             ORDER BY intent_at ASC, command_id ASC
             LIMIT ?
            """,
            (*normalized_states, bounded_limit),
        ).fetchall()
        return [_activity_command_row_to_dict(row) for row in rows]

    def update_activity_command_state(
        self,
        command_id: str,
        *,
        next_state: str,
        error_reason: str = "",
        result_event_id: int | None = None,
    ) -> dict[str, Any]:
        stable_command = _text(command_id)
        stable_next_state = _text(next_state)
        if not stable_command or not stable_next_state:
            return {}
        row = self._conn.execute(
            "SELECT * FROM activity_commands WHERE command_id = ?",
            (stable_command,),
        ).fetchone()
        if row is None:
            return {}
        current_state = _row_text(row, "state", 5)
        if not is_legal_transition(current_state, stable_next_state):
            return {}
        next_error_reason = _text(error_reason) or _row_text(row, "error_reason", 8)
        next_result_event_id = (
            result_event_id
            if result_event_id is not None
            else _row_value(row, "result_event_id", 7)
        )
        now = time.time()
        cursor = self._conn.execute(
            """
            UPDATE activity_commands
               SET state = ?,
                   state_changed_at = ?,
                   result_event_id = ?,
                   error_reason = ?
             WHERE command_id = ?
               AND state = ?
            """,
            (
                stable_next_state,
                now,
                next_result_event_id,
                next_error_reason,
                stable_command,
                current_state,
            ),
        )
        if int(cursor.rowcount or 0) == 0:
            return {}
        return self.get_activity_command(stable_command)

    def list_activity_commands_for_activity(
        self,
        activity_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        stable_activity = _text(activity_id)
        if not stable_activity:
            return []
        rows = self._conn.execute(
            """
            SELECT *
              FROM activity_commands
             WHERE activity_id = ?
             ORDER BY intent_at ASC, command_id ASC
             LIMIT ?
            """,
            (stable_activity, _activity_command_limit(limit, 200)),
        ).fetchall()
        return [_activity_command_row_to_dict(row) for row in rows]

    def migrate_legacy_activities_kind_mission_check(self) -> bool:
        row = self._conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'activities'
            """
        ).fetchone()
        sql = str(_row_value(row, "sql", 0) or "")
        if not sql or "'mission'" in sql:
            return False
        columns = self._conn.execute('PRAGMA table_info("activities")').fetchall()
        live_columns = [
            str(_row_value(column, "name", 1) or "")
            for column in columns
            if str(_row_value(column, "name", 1) or "")
        ]
        desired_columns = [
            "activity_id",
            "conversation_id",
            "parent_activity_id",
            "kind",
            "target_profile_id",
            "target_team_id",
            "target_mission_id",
            "status",
            "prompt_summary",
            "result_summary",
            "result_json",
            "started_at",
            "completed_at",
            "notify_parent",
            "read_at",
            "created_at",
            "updated_at",
        ]
        copy_columns = [column for column in desired_columns if column in live_columns]
        if not copy_columns:
            return False
        self._conn.execute("DROP INDEX IF EXISTS idx_activities_conv")
        self._conn.execute("DROP INDEX IF EXISTS idx_activities_parent")
        self._conn.execute("DROP INDEX IF EXISTS idx_activities_mission")
        self._conn.execute("DROP TABLE IF EXISTS activities_legacy_kind_check")
        self._conn.execute("ALTER TABLE activities RENAME TO activities_legacy_kind_check")
        self._conn.execute(
            """
            CREATE TABLE activities (
                activity_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                parent_activity_id TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('chat', 'agent_dispatch', 'team_dispatch', 'member_chat', 'mission')),
                target_profile_id TEXT,
                target_team_id TEXT,
                target_mission_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'interrupted')),
                prompt_summary TEXT,
                result_summary TEXT,
                result_json TEXT,
                started_at REAL,
                completed_at REAL,
                notify_parent INTEGER NOT NULL DEFAULT 1,
                read_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        column_sql = ", ".join(f'"{column}"' for column in copy_columns)
        self._conn.execute(
            f"INSERT INTO activities ({column_sql}) "
            f"SELECT {column_sql} FROM activities_legacy_kind_check"
        )
        self._conn.execute("DROP TABLE activities_legacy_kind_check")
        return True

    # ------------------------------------------------------------------
    # Legacy Activity table — current gateway/UI read model.
    # ------------------------------------------------------------------

    def create_legacy_activity(
        self,
        *,
        activity_id: str,
        conversation_id: str,
        kind: str,
        parent_activity_id: str | None = None,
        target_profile_id: str | None = None,
        target_team_id: str | None = None,
        target_mission_id: str | None = None,
        status: str = "pending",
        prompt_summary: str | None = None,
        notify_parent: bool = True,
    ) -> dict[str, Any]:
        stable_aid = _text(activity_id)
        stable_conversation = _text(conversation_id)
        stable_kind = _text(kind)
        stable_status = _text(status) or "pending"
        if not stable_aid:
            raise ValueError("activity_id required")
        if not stable_conversation:
            raise ValueError("conversation_id required")
        if not stable_kind:
            raise ValueError("kind required")
        if stable_kind not in _LEGACY_ACTIVITY_KINDS:
            raise sqlite3.IntegrityError("invalid activity kind")
        if stable_status not in {"pending", "running", "completed", "failed", "cancelled", "interrupted"}:
            raise sqlite3.IntegrityError("invalid activity status")
        now = time.time()
        started_at = now if stable_status == "running" else None
        completed_at = now if stable_status in _TERMINAL_ACTIVITY_STATUSES else None
        self._conn.execute(
            """
            INSERT INTO activities (
                activity_id, conversation_id, parent_activity_id, kind,
                target_profile_id, target_team_id, target_mission_id, status,
                prompt_summary, result_summary, result_json,
                started_at, completed_at, notify_parent, read_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?, ?)
            """,
            (
                stable_aid,
                stable_conversation,
                _optional_text(parent_activity_id),
                stable_kind,
                _optional_text(target_profile_id),
                _optional_text(target_team_id),
                _optional_text(target_mission_id),
                stable_status,
                _optional_text(prompt_summary),
                started_at,
                completed_at,
                1 if notify_parent else 0,
                now,
                now,
            ),
        )
        return self.get_legacy_activity(stable_aid) or {}

    def ensure_legacy_mission_activity(
        self,
        *,
        conversation_id: str,
        mission_id: str,
        status: str = "running",
        prompt_summary: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        stable_conversation = _text(conversation_id)
        stable_mission = _text(mission_id)
        if not stable_conversation or not stable_mission:
            return {}
        stable_status = _text(status) or "running"
        if stable_status not in {"pending", "running", "completed", "failed", "cancelled", "interrupted"}:
            stable_status = "running"
        timestamp = float(now if now is not None else time.time())
        activity_id = _mission_activity_id(stable_mission)
        existing = self._conn.execute(
            """
            SELECT activity_id, status, created_at, started_at
            FROM activities
            WHERE kind = 'mission' AND target_mission_id = ?
            LIMIT 1
            """,
            (stable_mission,),
        ).fetchone()
        existing_activity_id = _row_text(existing, "activity_id", 0) if existing is not None else ""
        existing_status = _row_text(existing, "status", 1) if existing is not None else ""
        if existing_activity_id and existing_status in _TERMINAL_ACTIVITY_STATUSES:
            return self.get_legacy_activity(existing_activity_id) or {}
        row_created_at = (
            float(_row_value(existing, "created_at", 2) or timestamp)
            if existing is not None
            else timestamp
        )
        started_at = (
            float(_row_value(existing, "started_at", 3) or timestamp)
            if existing is not None and stable_status == "running"
            else timestamp if stable_status == "running" else None
        )
        completed_at = timestamp if stable_status in _TERMINAL_ACTIVITY_STATUSES else None
        self._conn.execute(
            """
            INSERT INTO activities (
                activity_id, conversation_id, parent_activity_id, kind,
                target_profile_id, target_team_id, target_mission_id, status,
                prompt_summary, result_summary, result_json,
                started_at, completed_at, notify_parent, read_at,
                created_at, updated_at
            )
            VALUES (?, ?, NULL, 'mission', NULL, NULL, ?, ?, ?, NULL, NULL, ?, ?, 1, NULL, ?, ?)
            ON CONFLICT(activity_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                kind = 'mission',
                target_mission_id = excluded.target_mission_id,
                status = CASE
                    WHEN activities.status IN ('completed', 'failed', 'cancelled', 'interrupted')
                    THEN activities.status
                    ELSE excluded.status
                END,
                prompt_summary = COALESCE(NULLIF(excluded.prompt_summary, ''), activities.prompt_summary),
                started_at = COALESCE(activities.started_at, excluded.started_at),
                completed_at = CASE
                    WHEN activities.status IN ('completed', 'failed', 'cancelled', 'interrupted')
                    THEN activities.completed_at
                    ELSE excluded.completed_at
                END,
                updated_at = excluded.updated_at
            """,
            (
                existing_activity_id or activity_id,
                stable_conversation,
                stable_mission,
                stable_status,
                _optional_text(prompt_summary),
                started_at,
                completed_at,
                row_created_at,
                timestamp,
            ),
        )
        return self.get_legacy_activity_for_mission(stable_mission) or {}

    def bind_legacy_activity_to_mission(
        self,
        *,
        activity_id: str,
        conversation_id: str,
        mission_id: str,
        target_team_id: str | None = None,
        prompt_summary: str | None = None,
        status: str = "running",
    ) -> dict[str, Any]:
        stable_aid = _text(activity_id)
        stable_conversation = _text(conversation_id)
        stable_mission = _text(mission_id)
        stable_status = _text(status) or "running"
        if not stable_aid:
            raise ValueError("activity_id required")
        if not stable_conversation:
            raise ValueError("conversation_id required")
        if not stable_mission:
            raise ValueError("mission_id required")
        if stable_status not in {"pending", "running", "completed", "failed", "cancelled", "interrupted"}:
            stable_status = "running"
        now = time.time()
        started_at = now if stable_status == "running" else None
        row = self._conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?",
            (stable_aid,),
        ).fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO activities (
                    activity_id, conversation_id, parent_activity_id, kind,
                    target_profile_id, target_team_id, target_mission_id, status,
                    prompt_summary, result_summary, result_json,
                    started_at, completed_at, notify_parent, read_at,
                    created_at, updated_at
                )
                VALUES (?, ?, NULL, 'team_dispatch', NULL, ?, ?, ?, ?, NULL, NULL, ?, NULL, 1, NULL, ?, ?)
                """,
                (
                    stable_aid,
                    stable_conversation,
                    _optional_text(target_team_id),
                    stable_mission,
                    stable_status,
                    _optional_text(prompt_summary),
                    started_at,
                    now,
                    now,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE activities
                   SET conversation_id = ?,
                       kind = CASE
                           WHEN kind IN ('team_dispatch', 'mission') THEN kind
                           ELSE 'team_dispatch'
                       END,
                       target_team_id = COALESCE(?, target_team_id),
                       target_mission_id = ?,
                       status = CASE
                           WHEN status IN ('completed', 'failed', 'cancelled', 'interrupted') THEN status
                           WHEN ? = 'running' THEN 'running'
                           ELSE status
                       END,
                       prompt_summary = COALESCE(NULLIF(?, ''), prompt_summary),
                       started_at = CASE
                           WHEN ? = 'running' THEN COALESCE(started_at, ?)
                           ELSE started_at
                       END,
                       updated_at = ?
                 WHERE activity_id = ?
                """,
                (
                    stable_conversation,
                    _optional_text(target_team_id),
                    stable_mission,
                    stable_status,
                    _optional_text(prompt_summary),
                    stable_status,
                    started_at,
                    now,
                    stable_aid,
                ),
            )
        return self.get_legacy_activity(stable_aid) or {}

    def get_legacy_activity_for_mission(self, mission_id: str) -> dict[str, Any] | None:
        stable_mission = _text(mission_id)
        if not stable_mission:
            return None
        row = self._conn.execute(
            """
            SELECT *
            FROM activities
            WHERE kind = 'mission' AND target_mission_id = ?
            ORDER BY created_at ASC, activity_id ASC
            LIMIT 1
            """,
            (stable_mission,),
        ).fetchone()
        return _legacy_activity_row(row)

    def list_legacy_active_mission_activities(self, conversation_id: str) -> list[dict[str, Any]]:
        stable_conversation = _text(conversation_id)
        if not stable_conversation:
            return []
        rows = self._conn.execute(
            """
            SELECT *
            FROM activities
            WHERE conversation_id = ?
              AND kind = 'mission'
              AND status NOT IN ('completed', 'failed', 'cancelled', 'interrupted')
            ORDER BY started_at ASC, created_at ASC, activity_id ASC
            """,
            (stable_conversation,),
        ).fetchall()
        return [_legacy_activity_row(row) or {} for row in rows]

    def mark_legacy_mission_activity_terminal(
        self,
        *,
        mission_id: str,
        status: str,
        result_summary: str | None = None,
        result_json: Any = None,
        now: float | None = None,
    ) -> bool:
        stable_mission = _text(mission_id)
        stable_status = _text(status)
        if not stable_mission or stable_status not in _TERMINAL_ACTIVITY_STATUSES:
            return False
        timestamp = float(now if now is not None else time.time())
        cursor = self._conn.execute(
            """
            UPDATE activities
               SET status = ?,
                   result_summary = COALESCE(?, result_summary),
                   result_json = COALESCE(?, result_json),
                   completed_at = COALESCE(completed_at, ?),
                   updated_at = ?
             WHERE kind = 'mission'
               AND target_mission_id = ?
               AND status NOT IN ('completed', 'failed', 'cancelled', 'interrupted')
            """,
            (
                stable_status,
                _optional_text(result_summary),
                _json_text(result_json) if result_json is not None else None,
                timestamp,
                timestamp,
                stable_mission,
            ),
        )
        return bool(cursor.rowcount)

    def update_legacy_activity_status(
        self,
        activity_id: str,
        status: str,
        *,
        target_profile_id: str | None = None,
        target_team_id: str | None = None,
        target_mission_id: str | None = None,
        result_summary: str | None = None,
        result_json: Any = None,
        started_at: float | None = None,
        completed_at: float | None = None,
    ) -> bool:
        stable_aid = _text(activity_id)
        stable_status = _text(status)
        if not stable_aid:
            raise ValueError("activity_id required")
        if not stable_status:
            raise ValueError("status required")
        if stable_status not in {"pending", "running", "completed", "failed", "cancelled", "interrupted"}:
            raise sqlite3.IntegrityError("invalid activity status")
        now = time.time()
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [stable_status, now]
        for column, value in (
            ("target_profile_id", target_profile_id),
            ("target_team_id", target_team_id),
            ("target_mission_id", target_mission_id),
            ("result_summary", result_summary),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(_optional_text(value))
        if result_json is not None:
            assignments.append("result_json = ?")
            params.append(_json_text(result_json))
        if started_at is not None:
            assignments.append("started_at = ?")
            params.append(float(started_at))
        if completed_at is not None:
            assignments.append("completed_at = ?")
            params.append(float(completed_at))
        params.append(stable_aid)
        allowed_previous = _LEGACY_ACTIVITY_STATUS_ALLOWED_PREVIOUS.get(stable_status)
        where = "activity_id = ?"
        update_params = list(params)
        if allowed_previous is not None:
            placeholders = ", ".join("?" for _ in allowed_previous)
            where = f"{where} AND status IN ({placeholders})"
            update_params.extend(allowed_previous)
        cursor = self._conn.execute(
            f"UPDATE activities SET {', '.join(assignments)} WHERE {where}",
            tuple(update_params),
        )
        return cursor.rowcount > 0

    def mark_legacy_activity_completed(
        self,
        activity_id: str,
        *,
        result_summary: str,
        result_json: Any,
    ) -> bool:
        return self.update_legacy_activity_status(
            activity_id,
            "completed",
            result_summary=result_summary,
            result_json=result_json,
            completed_at=time.time(),
        )

    def mark_legacy_activity_failed(
        self,
        activity_id: str,
        *,
        error_message: str,
        result_json: Any = None,
    ) -> bool:
        return self.update_legacy_activity_status(
            activity_id,
            "failed",
            result_summary=error_message,
            result_json=result_json,
            completed_at=time.time(),
        )

    def mark_legacy_activity_cancelled(
        self,
        activity_id: str,
        *,
        result_summary: str | None = None,
        result_json: Any = None,
    ) -> bool:
        return self.update_legacy_activity_status(
            activity_id,
            "cancelled",
            result_summary=result_summary,
            result_json=result_json,
            completed_at=time.time(),
        )

    def mark_legacy_activity_read(self, activity_id: str) -> bool:
        stable_aid = _text(activity_id)
        if not stable_aid:
            raise ValueError("activity_id required")
        now = time.time()
        cursor = self._conn.execute(
            """
            UPDATE activities
               SET read_at = ?, updated_at = ?
             WHERE activity_id = ?
            """,
            (now, now, stable_aid),
        )
        return cursor.rowcount > 0

    def list_legacy_activities(
        self,
        conversation_id: str,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        stable_conversation = _text(conversation_id)
        if not stable_conversation:
            return []
        params: list[Any] = [stable_conversation]
        where = ["conversation_id = ?"]
        if status is not None:
            where.append("status = ?")
            params.append(_text(status))
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self._conn.execute(
            "SELECT * FROM activities "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at ASC, activity_id ASC"
            f"{limit_sql}",
            tuple(params),
        ).fetchall()
        return [_legacy_activity_row(row) or {} for row in rows]

    def list_legacy_unread_completions(
        self,
        parent_activity_id: str | None = None,
        *,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["status IN ('completed', 'failed')", "read_at IS NULL"]
        params: list[Any] = []
        if parent_activity_id is not None:
            where.append("parent_activity_id = ?")
            params.append(_text(parent_activity_id))
        if conversation_id is not None:
            where.append("conversation_id = ?")
            params.append(_text(conversation_id))
        rows = self._conn.execute(
            "SELECT * FROM activities "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(completed_at, updated_at) ASC, activity_id ASC",
            tuple(params),
        ).fetchall()
        return [_legacy_activity_row(row) or {} for row in rows]

    def get_legacy_unread_completion_count(
        self,
        *,
        parent_activity_id: str | None = None,
        conversation_id: str | None = None,
    ) -> int:
        where = ["status IN ('completed', 'failed')", "read_at IS NULL"]
        params: list[Any] = []
        if parent_activity_id is not None:
            where.append("parent_activity_id = ?")
            params.append(_text(parent_activity_id))
        if conversation_id is not None:
            where.append("conversation_id = ?")
            params.append(_text(conversation_id))
        row = self._conn.execute(
            "SELECT COUNT(1) AS count FROM activities "
            f"WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()
        return int(_row_value(row, "count", 0) or 0) if row is not None else 0

    def get_legacy_activity(self, activity_id: str) -> dict[str, Any] | None:
        stable_aid = _text(activity_id)
        if not stable_aid:
            return None
        row = self._conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?",
            (stable_aid,),
        ).fetchone()
        return _legacy_activity_row(row)


def _row_to_mission(row: Any) -> Mission:
    def _g(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    return Mission(
        mission_id=str(_g("mission_id", 0) or ""),
        session_id=str(_g("session_id", 1) or ""),
        title=str(_g("title", 2) or ""),
        status=str(_g("status", 3) or "pending"),
        created_at=float(_g("created_at", 4) or 0),
        updated_at=float(_g("updated_at", 5) or 0),
        plan_json=str(_g("plan_json", 6) or ""),
    )


def _row_to_node(row: Any) -> MissionNode:
    def _g(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    return MissionNode(
        mission_id=str(_g("mission_id", 0) or ""),
        node_id=str(_g("node_id", 1) or ""),
        kind=str(_g("kind", 2) or "task"),
        title=str(_g("title", 3) or ""),
        status=str(_g("status", 4) or "pending"),
        bound_run_id=str(_g("bound_run_id", 5) or ""),
        plan_json=str(_g("plan_json", 6) or ""),
        updated_at=float(_g("updated_at", 7) or 0),
    )


def _row_to_edge(row: Any) -> MissionEdge:
    def _g(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    return MissionEdge(
        mission_id=str(_g("mission_id", 0) or ""),
        from_node_id=str(_g("from_node_id", 1) or ""),
        to_node_id=str(_g("to_node_id", 2) or ""),
        kind=str(_g("kind", 3) or "sequence"),
    )


def _row_to_activity(row: Any) -> Activity:
    def _g(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    metadata_raw = _g("metadata_json", 10) or ""
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    completed_at_raw = _g("completed_at", 9)
    return Activity(
        activity_id=str(_g("activity_id", 0) or ""),
        session_id=str(_g("session_id", 1) or ""),
        kind=str(_g("kind", 2) or ""),  # type: ignore[arg-type]
        activity_seq=int(_g("activity_seq", 3) or 0),
        status=str(_g("status", 4) or "pending"),  # type: ignore[arg-type]
        target_id=str(_g("target_id", 5) or ""),
        prompt_summary=str(_g("prompt_summary", 6) or ""),
        result_summary=str(_g("result_summary", 7) or ""),
        started_at=float(_g("started_at", 8) or 0),
        completed_at=float(completed_at_raw) if completed_at_raw is not None else None,
        metadata=metadata,
    )


def _legacy_activity_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row) if isinstance(row, sqlite3.Row) else {}
    if not item:
        return None
    item["notify_parent"] = bool(item.get("notify_parent"))
    return item


def _activity_command_json(value: dict[str, Any] | None) -> str:
    if not isinstance(value, dict):
        value = {}
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _activity_command_json_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _activity_command_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    item["payload"] = _activity_command_json_dict(item.get("payload_json"))
    item["metadata"] = _activity_command_json_dict(item.get("metadata_json"))
    item["error_reason"] = str(item.get("error_reason") or "")
    return item


def _activity_command_limit(limit: int, default: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, 5000))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _mission_activity_id(mission_id: str) -> str:
    stable = _text(mission_id)
    if not stable:
        raise ValueError("mission_id required")
    return f"mission:{stable}"


def _row_value(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return row[key]
    return row[index]


def _row_text(row: Any, key: str, index: int) -> str:
    return str(_row_value(row, key, index) or "")


__all__ = [
    "Activity",
    "ActivityKind",
    "ActivitySpec",
    "ActivityStatus",
    "EdgeSpec",
    "Mission",
    "MissionEdge",
    "MissionGraph",
    "MissionNode",
    "MissionSpec",
    "NodeSpec",
    "NodeStatus",
    "TeamMissionRepo",
    "TeamMissionRepoImpl",
]
