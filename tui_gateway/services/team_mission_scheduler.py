from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from hermes_team_mission_modes import strategy_for_mode

logger = logging.getLogger(__name__)

_TERMINAL_DEPENDENCY_STATUSES = {"completed", "verified"}
_TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
_ACTIVE_EXECUTION_NODE_STATUSES = {"starting", "running", "waiting_approval"}
_NON_EXECUTION_NODE_KINDS = {"root", "approval_gate"}
_DEFAULT_MAX_PARALLEL_NODES = 3
_HARD_MAX_PARALLEL_NODES = 5


def _node_id(node: dict[str, Any]) -> str:
    return str((node or {}).get("node_id") or (node or {}).get("id") or "").strip()


def _node_status(node: dict[str, Any]) -> str:
    return str((node or {}).get("status") or "").strip()


def _mission_status(mission: dict[str, Any]) -> str:
    return str((mission or {}).get("status") or "").strip()


def _graph_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = (graph or {}).get("nodes")
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def _node_kind(node: dict[str, Any]) -> str:
    return str((node or {}).get("kind") or "").strip()


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = (node or {}).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _mission_metadata(mission: dict[str, Any]) -> dict[str, Any]:
    metadata = (mission or {}).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _task_id_from_metadata(metadata: dict[str, Any]) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), dict) else {}
    return str(
        metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
        or metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
        or ""
    ).strip()


def _mapping_value(source: dict[str, Any], *keys: str) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        if key in source:
            return source.get(key)
    return None


def _mission_policy(mission: dict[str, Any]) -> dict[str, Any]:
    metadata = _mission_metadata(mission)
    policy = _mapping_value(metadata, "policy")
    if isinstance(policy, dict):
        return policy
    policy = _mapping_value(mission, "policy")
    return policy if isinstance(policy, dict) else {}


def _max_parallel_nodes(mission: dict[str, Any]) -> int:
    policy = _mission_policy(mission)
    raw = _mapping_value(policy, "maxParallelNodes", "max_parallel_nodes", "max_parallel")
    if raw is None:
        metadata = _mission_metadata(mission)
        raw = _mapping_value(metadata, "maxParallelNodes", "max_parallel_nodes", "max_parallel")
    if raw is None:
        raw = _mapping_value(mission, "maxParallelNodes", "max_parallel_nodes", "max_parallel")
    return _bounded_limit(raw, default=_DEFAULT_MAX_PARALLEL_NODES, maximum=_HARD_MAX_PARALLEL_NODES)


def _is_execution_node(node: dict[str, Any]) -> bool:
    return bool(_node_id(node)) and _node_kind(node) not in _NON_EXECUTION_NODE_KINDS


def _active_execution_node_count(graph: dict[str, Any]) -> int:
    return sum(
        1
        for node in _graph_nodes(graph)
        if _is_execution_node(node) and _node_status(node) in _ACTIVE_EXECUTION_NODE_STATUSES
    )


def _approval_pending(mission: dict[str, Any], graph: dict[str, Any]) -> bool:
    if str((mission or {}).get("mode") or "") != "supervised_mission":
        return False
    return any(
        str(node.get("kind") or "") == "approval_gate"
        and _node_status(node) not in _TERMINAL_DEPENDENCY_STATUSES
        for node in _graph_nodes(graph)
    )


def _bounded_limit(value: Any, *, default: int = 10, maximum: int = 50) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


class TeamMissionReadyScheduler:
    """Start dependency-ready Team Mission nodes through the gateway run path."""

    def __init__(
        self,
        *,
        db: Any,
        start_node: Callable[[Any, dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._db = db
        self._start_node = start_node

    def schedule_ready_nodes(
        self,
        *,
        mission_id: str,
        rid: Any = None,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        trigger: str = "",
    ) -> dict[str, Any]:
        normalized_mission_id = str(mission_id or "").strip()
        if not normalized_mission_id:
            return {}
        schedule_params = dict(params or {})
        reduced = self._db.reduce_team_mission_graph(normalized_mission_id)
        if not reduced:
            return {}
        graph = reduced.get("graph") if isinstance(reduced.get("graph"), dict) else self._db.get_team_mission_graph(normalized_mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else {}
        if not isinstance(mission, dict):
            return {}
        if _mission_status(mission) in _TERMINAL_MISSION_STATUSES:
            return {
                "mission_id": normalized_mission_id,
                "ready_node_ids": [],
                "max_parallel_nodes": _max_parallel_nodes(mission),
                "active_node_count": _active_execution_node_count(graph),
                "available_slots": 0,
                "started": [],
                "skipped": [{"reason": "mission_terminal", "status": _mission_status(mission)}],
                "errors": [],
                "graph": graph,
            }
        ready_node_ids = self._ready_node_ids(
            graph=graph,
            reducer_ready_node_ids=list(reduced.get("ready_node_ids") or []),
            task_id=str(schedule_params.get("task_id") or schedule_params.get("taskId") or _task_id_from_metadata(_mission_metadata(mission)) or "").strip(),
        )
        max_parallel_nodes = _max_parallel_nodes(mission)
        active_node_count = _active_execution_node_count(graph)
        available_slots = max(0, max_parallel_nodes - active_node_count)
        if _approval_pending(mission, graph):
            return {
                "mission_id": normalized_mission_id,
                "ready_node_ids": [],
                "max_parallel_nodes": max_parallel_nodes,
                "active_node_count": active_node_count,
                "available_slots": available_slots,
                "started": [],
                "skipped": [
                    {
                        "reason": "approval_pending",
                        "node_ids": ready_node_ids,
                    }
                ] if ready_node_ids else [],
                "errors": [],
                "graph": graph,
            }
        started: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        requested_limit = _bounded_limit(limit if limit is not None else schedule_params.get("limit"))
        node_limit = min(requested_limit, available_slots)
        if node_limit <= 0:
            return {
                "mission_id": normalized_mission_id,
                "ready_node_ids": ready_node_ids,
                "max_parallel_nodes": max_parallel_nodes,
                "active_node_count": active_node_count,
                "available_slots": available_slots,
                "started": [],
                "skipped": [
                    {
                        "reason": "concurrency_limit",
                        "node_ids": ready_node_ids,
                    }
                ] if ready_node_ids else [],
                "errors": [],
                "graph": self._db.get_team_mission_graph(normalized_mission_id),
            }
        if len(ready_node_ids) > node_limit:
            skipped.append({
                "reason": "concurrency_limit",
                "node_ids": ready_node_ids[node_limit:],
            })
        for node_id in ready_node_ids[:node_limit]:
            if dry_run:
                started.append({"node_id": node_id, "dry_run": True})
                continue
            claimed = self._claim_node(normalized_mission_id, node_id, trigger=trigger)
            claimed_status = _node_status(claimed)
            if claimed_status != "starting":
                skipped.append({
                    "node_id": node_id,
                    "reason": "not_claimed",
                    "status": claimed_status,
                })
                continue
            response = self._start_node(
                rid,
                self._node_start_params(
                    base_params=schedule_params,
                    mission_id=normalized_mission_id,
                    node_id=node_id,
                    trigger=trigger,
                    ready_count=len(ready_node_ids),
                ),
            )
            if isinstance(response, dict) and response.get("error"):
                errors.append({"node_id": node_id, "error": response.get("error")})
                continue
            started.append({"node_id": node_id, "result": response.get("result") if isinstance(response, dict) else {}})
        return {
            "mission_id": normalized_mission_id,
            "ready_node_ids": ready_node_ids,
            "max_parallel_nodes": max_parallel_nodes,
            "active_node_count": active_node_count,
            "available_slots": available_slots,
            "started": started,
            "skipped": skipped,
            "errors": errors,
            "graph": self._db.get_team_mission_graph(normalized_mission_id),
        }

    def _ready_node_ids(
        self,
        *,
        graph: dict[str, Any],
        reducer_ready_node_ids: list[str],
        task_id: str = "",
    ) -> list[str]:
        mission = graph.get("mission") if isinstance(graph, dict) else {}
        mode = str((mission or {}).get("mode") or "supervised_mission")
        strategy = strategy_for_mode(mode)
        selected = list(strategy.select_ready_nodes(graph))
        reducer_ready = {
            str(node_id or "").strip()
            for node_id in reducer_ready_node_ids
            if str(node_id or "").strip()
        }
        if reducer_ready:
            selected = [node_id for node_id in selected if node_id in reducer_ready]
        nodes_by_id = {
            _node_id(node): node
            for node in _graph_nodes(graph)
            if _node_id(node)
        }
        ready_node_ids: list[str] = []
        seen: set[str] = set()
        for node_id in selected:
            normalized_node_id = str(node_id or "").strip()
            if not normalized_node_id or normalized_node_id in seen:
                continue
            node = nodes_by_id.get(normalized_node_id)
            if not node or _node_status(node) != "ready":
                continue
            metadata = _node_metadata(node)
            if task_id and _task_id_from_metadata(metadata) != task_id:
                continue
            if metadata.get("manual_start") is True:
                continue
            seen.add(normalized_node_id)
            ready_node_ids.append(normalized_node_id)
        return ready_node_ids

    def _claim_node(self, mission_id: str, node_id: str, *, trigger: str = "") -> dict[str, Any]:
        claim = getattr(self._db, "claim_team_mission_node_start", None)
        if callable(claim):
            return claim(
                mission_id=mission_id,
                node_id=node_id,
                metadata={
                    "scheduler_trigger": str(trigger or "team_mission.scheduler"),
                },
            )
        logger.debug("Team Mission scheduler db has no claim method; starting without atomic claim")
        return self._db.get_team_mission_node(mission_id, node_id)

    @staticmethod
    def _node_start_params(
        *,
        base_params: dict[str, Any],
        mission_id: str,
        node_id: str,
        trigger: str,
        ready_count: int,
    ) -> dict[str, Any]:
        explicit_run_id = str(base_params.get("client_run_id") or base_params.get("clientRunId") or "").strip()
        explicit_turn_id = str(base_params.get("turn_id") or base_params.get("turnId") or "").strip()
        use_explicit_ids = ready_count == 1 and bool(explicit_run_id)
        return {
            **base_params,
            "mission_id": mission_id,
            "node_id": node_id,
            "client_run_id": explicit_run_id if use_explicit_ids else uuid.uuid4().hex,
            "turn_id": explicit_turn_id if use_explicit_ids and explicit_turn_id else uuid.uuid4().hex,
            "scheduler_trigger": str(trigger or "team_mission.scheduler"),
        }
