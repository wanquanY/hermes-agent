"""TeamMissionOrchestrator — L3 mission-graph execution coordinator (spec §3).

Sits above ``TeamMissionRepoImpl`` and coordinates node execution against
runs. The repository owns persistence; the orchestrator owns the transition
policy (which node can start next, when a node is complete, how activity
progress interacts with node status).

The mission graph is a DAG of nodes with sequence edges. A node can run when
every incoming edge's ``from_node`` is terminal. The orchestrator answers
"which nodes are ready to run" and "advance the graph after a bound run
terminates" — mission progress becomes a pure function of node status +
graph shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from hermes_agent.repositories.team_mission_repo import (
    ActivityKind,
    ActivitySpec,
    Mission,
    MissionGraph,
    MissionNode,
    NodeStatus,
    TeamMissionRepoImpl,
)


_logger = logging.getLogger(__name__)


TERMINAL_NODE_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)
RUNNING_NODE_STATUSES: frozenset[str] = frozenset(
    {"running", "waiting_approval"}
)


@dataclass(frozen=True)
class MissionAdvanceOutcome:
    """Result of ``advance_after_terminal_node`` — what changed on the graph."""

    mission_id: str
    terminated_node_id: str
    newly_ready_nodes: tuple[str, ...]
    activity_ids_recorded: tuple[str, ...]


class TeamMissionOrchestrator:
    """Coordinates mission graph execution.

    Interface:

        orch.ready_nodes(mission_id) -> list[MissionNode]
            * Every node with status='pending' whose incoming edges all
              point to a terminal predecessor. This is what a scheduler
              picks up next.

        orch.mark_node_running(mission_id, node_id, run_id)
            * Bind the node to the run and flip its status to 'running'.

        orch.advance_after_terminal_node(
            mission_id, node_id, terminal_status
        ) -> MissionAdvanceOutcome
            * Called after the bound run terminated. Sets the node terminal,
              logs a dispatch_completion activity so the frontend items SSoT
              sees mission progress, and returns the set of nodes that are
              newly ready to run.

        orch.mission_terminal_status(mission_id) -> str | None
            * Aggregates node status into a mission-level judgment:
              - "failed" if any node failed
              - "cancelled" if all terminal nodes are cancelled
              - "completed" if every node is terminal and >=1 completed
              - None otherwise (mission still running)
    """

    def __init__(self, repo: TeamMissionRepoImpl) -> None:
        self._repo = repo

    # ------------------------------------------------------------------

    def ready_nodes(self, mission_id: str) -> list[MissionNode]:
        graph = self._repo.get_graph(mission_id)
        if graph is None:
            return []
        return _compute_ready_nodes(graph)

    def mark_node_running(
        self,
        mission_id: str,
        node_id: str,
        run_id: str,
    ) -> None:
        stable_mission = str(mission_id or "").strip()
        stable_node = str(node_id or "").strip()
        stable_run = str(run_id or "").strip()
        if not stable_mission or not stable_node or not stable_run:
            raise ValueError("mission_id, node_id, run_id all required")
        self._repo.bind_run(stable_mission, stable_node, stable_run)
        self._repo.update_node_status(stable_mission, stable_node, "running")

    def advance_after_terminal_node(
        self,
        mission_id: str,
        node_id: str,
        terminal_status: NodeStatus,
        *,
        session_id: str = "",
        activity_seq_source: str = "",
    ) -> MissionAdvanceOutcome:
        stable_mission = str(mission_id or "").strip()
        stable_node = str(node_id or "").strip()
        status = str(terminal_status or "").strip().lower()
        if not stable_mission or not stable_node:
            raise ValueError("mission_id, node_id required")
        if status not in TERMINAL_NODE_STATUSES:
            raise ValueError(
                f"terminal_status must be one of {sorted(TERMINAL_NODE_STATUSES)}, "
                f"got {terminal_status!r}"
            )

        self._repo.update_node_status(stable_mission, stable_node, status)  # type: ignore[arg-type]

        # Record a ``dispatch_completion`` activity so the frontend items SSoT
        # picks up mission progress in seq order (spec §6.5 activity_seq
        # shares the run_events domain).
        activity_ids: list[str] = []
        stable_session = str(session_id or activity_seq_source or "").strip()
        if stable_session:
            spec = ActivitySpec(
                activity_id=f"activity-completion-{stable_mission}-{stable_node}",
                session_id=stable_session,
                kind="dispatch_completion",
                target_id=stable_node,
                prompt_summary=f"mission {stable_mission!r} node {stable_node!r} → {status}",
                metadata={"mission_id": stable_mission, "node_id": stable_node},
            )
            try:
                activity = self._repo.append_activity(stable_session, spec)
                activity_ids.append(activity.activity_id)
            except Exception as exc:
                # spec §J11 — recoverable: activity logging is best-effort
                # observability, not a mission-blocking invariant.
                _logger.warning(
                    "TeamMissionOrchestrator activity append failed for %s/%s: %s",
                    stable_mission,
                    stable_node,
                    exc,
                )

        graph = self._repo.get_graph(stable_mission)
        if graph is None:
            return MissionAdvanceOutcome(
                mission_id=stable_mission,
                terminated_node_id=stable_node,
                newly_ready_nodes=(),
                activity_ids_recorded=tuple(activity_ids),
            )

        newly_ready = tuple(
            n.node_id for n in _compute_ready_nodes(graph)
        )
        return MissionAdvanceOutcome(
            mission_id=stable_mission,
            terminated_node_id=stable_node,
            newly_ready_nodes=newly_ready,
            activity_ids_recorded=tuple(activity_ids),
        )

    def mission_terminal_status(self, mission_id: str) -> str | None:
        graph = self._repo.get_graph(mission_id)
        if graph is None or not graph.nodes:
            return None
        all_terminal = all(
            n.status in TERMINAL_NODE_STATUSES for n in graph.nodes
        )
        if not all_terminal:
            return None
        node_statuses = {n.status for n in graph.nodes}
        if "failed" in node_statuses:
            return "failed"
        if node_statuses == {"cancelled"}:
            return "cancelled"
        return "completed"


# ----------------------------------------------------------------------
# Pure functions on the graph
# ----------------------------------------------------------------------


def _compute_ready_nodes(graph: MissionGraph) -> list[MissionNode]:
    status_by_node = {n.node_id: n.status for n in graph.nodes}
    incoming: dict[str, list[str]] = {n.node_id: [] for n in graph.nodes}
    for edge in graph.edges:
        incoming.setdefault(edge.to_node_id, []).append(edge.from_node_id)

    ready: list[MissionNode] = []
    for node in graph.nodes:
        if node.status != "pending":
            continue
        predecessors = incoming.get(node.node_id, [])
        if all(
            status_by_node.get(pred, "pending") in TERMINAL_NODE_STATUSES
            for pred in predecessors
        ):
            ready.append(node)
    return ready


__all__ = [
    "MissionAdvanceOutcome",
    "RUNNING_NODE_STATUSES",
    "TERMINAL_NODE_STATUSES",
    "TeamMissionOrchestrator",
]
