"""Team-mission gateway methods (spec §4.4, §J8).

Two orchestration-facing methods:

* ``team_mission.ready_nodes``   — poll for the next scheduler batch
* ``team_mission.advance_node``  — record a node's terminal transition and
  discover any newly-ready follow-ups

Both are thin wrappers over ``TeamMissionOrchestrator`` — no business logic
lives here; the wrapper is the auth + schema surface only.
"""

from __future__ import annotations

from typing import Any

from hermes_agent.gateway.auth import requires_permission
from hermes_agent.gateway.error_codes import ErrorCode, MethodError
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.gateway.registry import MethodRegistry
from hermes_agent.orchestration import TeamMissionOrchestrator
from hermes_agent.repositories import (
    EdgeSpec,
    MissionSpec,
    NodeSpec,
    TeamMissionRepoImpl,
)


def _mission_projection(mission) -> dict[str, Any]:
    return {
        "mission_id": mission.mission_id,
        "session_id": mission.session_id,
        "title": mission.title,
        "status": mission.status,
        "created_at": mission.created_at,
        "updated_at": mission.updated_at,
        "plan_json": mission.plan_json,
    }


def _node_projection(node) -> dict[str, Any]:
    return {
        "mission_id": node.mission_id,
        "node_id": node.node_id,
        "kind": node.kind,
        "title": node.title,
        "status": node.status,
        "bound_run_id": node.bound_run_id,
        "plan_json": node.plan_json,
        "updated_at": node.updated_at,
    }


def _edge_projection(edge) -> dict[str, Any]:
    return {
        "mission_id": edge.mission_id,
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "kind": edge.kind,
    }


def make_method_team_mission_create(repo: TeamMissionRepoImpl):
    @requires_permission("team_mission.write")
    def method_team_mission_create(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        session_id = str(params.get("session_id") or "").strip()
        if not mission_id or not session_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "mission_id and session_id are required",
            )
        mission = repo.create_mission(
            session_id,
            MissionSpec(
                mission_id=mission_id,
                session_id=session_id,
                title=str(params.get("title") or ""),
                plan_json=str(params.get("plan_json") or ""),
                metadata=params.get("metadata") or {},
            ),
        )
        return _mission_projection(mission)

    return method_team_mission_create


def make_method_team_mission_get_graph(repo: TeamMissionRepoImpl):
    @requires_permission("team_mission.read", read_only=True)
    def method_team_mission_get_graph(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        if not mission_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS, "mission_id is required"
            )
        graph = repo.get_graph(mission_id)
        if graph is None:
            raise MethodError(
                ErrorCode.SESSION_NOT_FOUND,  # closest generic; MISSION_NOT_FOUND TBD
                f"mission {mission_id!r} not found",
            )
        return {
            "mission": _mission_projection(graph.mission),
            "nodes": [_node_projection(n) for n in graph.nodes],
            "edges": [_edge_projection(e) for e in graph.edges],
        }

    return method_team_mission_get_graph


def make_method_team_mission_get_node(repo: TeamMissionRepoImpl):
    @requires_permission("team_mission.read", read_only=True)
    def method_team_mission_get_node(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        node_id = str(params.get("node_id") or "").strip()
        if not mission_id or not node_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "mission_id and node_id are required",
            )
        node = repo.get_node(mission_id, node_id)
        if node is None:
            raise MethodError(
                ErrorCode.SESSION_NOT_FOUND,
                f"node {node_id!r} not found in mission {mission_id!r}",
            )
        return _node_projection(node)

    return method_team_mission_get_node


def make_method_team_mission_add_node(repo: TeamMissionRepoImpl):
    @requires_permission("team_mission.write")
    def method_team_mission_add_node(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        node_id = str(params.get("node_id") or "").strip()
        if not mission_id or not node_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "mission_id and node_id are required",
            )
        node = repo.add_node(
            NodeSpec(
                mission_id=mission_id,
                node_id=node_id,
                kind=str(params.get("kind") or "task"),
                title=str(params.get("title") or ""),
                plan_json=str(params.get("plan_json") or ""),
            )
        )
        return _node_projection(node)

    return method_team_mission_add_node


def make_method_team_mission_add_edge(repo: TeamMissionRepoImpl):
    @requires_permission("team_mission.write")
    def method_team_mission_add_edge(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        from_node_id = str(params.get("from_node_id") or "").strip()
        to_node_id = str(params.get("to_node_id") or "").strip()
        if not mission_id or not from_node_id or not to_node_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "mission_id, from_node_id, to_node_id all required",
            )
        edge = repo.add_edge(
            EdgeSpec(
                mission_id=mission_id,
                from_node_id=from_node_id,
                to_node_id=to_node_id,
                kind=str(params.get("kind") or "sequence"),
            )
        )
        return _edge_projection(edge)

    return method_team_mission_add_edge


def make_method_team_mission_bind_run(repo: TeamMissionRepoImpl):
    @requires_permission("team_mission.write")
    def method_team_mission_bind_run(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        node_id = str(params.get("node_id") or "").strip()
        run_id = str(params.get("run_id") or "").strip()
        if not mission_id or not node_id or not run_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "mission_id, node_id, run_id all required",
            )
        repo.bind_run(mission_id, node_id, run_id)
        return {
            "mission_id": mission_id,
            "node_id": node_id,
            "run_id": run_id,
        }

    return method_team_mission_bind_run


def make_method_team_mission_ready_nodes(orch: TeamMissionOrchestrator):
    @requires_permission("team_mission.read", read_only=True)
    def method_team_mission_ready_nodes(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        if not mission_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "mission_id is required")
        nodes = orch.ready_nodes(mission_id)
        return {
            "mission_id": mission_id,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "kind": n.kind,
                    "title": n.title,
                }
                for n in nodes
            ],
        }

    return method_team_mission_ready_nodes


def make_method_team_mission_advance_node(orch: TeamMissionOrchestrator):
    @requires_permission("team_mission.write")
    def method_team_mission_advance_node(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        mission_id = str(params.get("mission_id") or "").strip()
        node_id = str(params.get("node_id") or "").strip()
        target = str(params.get("terminal_status") or "").strip().lower()
        session_id = str(params.get("session_id") or "").strip()
        if not mission_id or not node_id or not target:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "mission_id, node_id, terminal_status all required",
            )
        try:
            outcome = orch.advance_after_terminal_node(
                mission_id,
                node_id,
                target,  # type: ignore[arg-type]
                session_id=session_id,
            )
        except ValueError as exc:
            raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        return {
            "mission_id": outcome.mission_id,
            "terminated_node_id": outcome.terminated_node_id,
            "newly_ready_nodes": list(outcome.newly_ready_nodes),
            "activity_ids_recorded": list(outcome.activity_ids_recorded),
        }

    return method_team_mission_advance_node


def register(registry: MethodRegistry, orch: TeamMissionOrchestrator) -> None:
    """Register orchestrator-facing methods (ready_nodes + advance_node)."""
    registry.register(
        "team_mission.ready_nodes",
        make_method_team_mission_ready_nodes(orch),
    )
    registry.register(
        "team_mission.advance_node",
        make_method_team_mission_advance_node(orch),
    )


def register_graph(registry: MethodRegistry, repo: TeamMissionRepoImpl) -> None:
    """Register the repository-facing mission graph CRUD methods."""
    registry.register("team_mission.create", make_method_team_mission_create(repo))
    registry.register(
        "team_mission.get_graph", make_method_team_mission_get_graph(repo)
    )
    registry.register(
        "team_mission.get_node", make_method_team_mission_get_node(repo)
    )
    registry.register(
        "team_mission.add_node", make_method_team_mission_add_node(repo)
    )
    registry.register(
        "team_mission.add_edge", make_method_team_mission_add_edge(repo)
    )
    registry.register(
        "team_mission.bind_run", make_method_team_mission_bind_run(repo)
    )
