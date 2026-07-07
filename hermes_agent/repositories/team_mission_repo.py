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


_ACTIVITY_KINDS = frozenset({
    "async_agent_dispatch",
    "async_team_dispatch",
    "team_mission_activity",
    "dispatch_completion",
})


@runtime_checkable
class TeamMissionRepo(Protocol):
    def create_mission(self, session_id: str, spec: MissionSpec) -> Mission: ...

    def get_graph(self, mission_id: str) -> MissionGraph | None: ...

    def get_node(self, mission_id: str, node_id: str) -> MissionNode | None: ...

    def bind_run(self, mission_id: str, node_id: str, run_id: str) -> None: ...

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
