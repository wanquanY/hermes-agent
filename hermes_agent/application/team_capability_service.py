"""Team capability snapshot orchestration across capability and mission aggregates."""

from __future__ import annotations

from typing import Any, Callable, Protocol

from hermes_agent.repositories.team_capability_repo import TeamCapabilityRepo
from hermes_team_capability_snapshot import text


class TeamMissionMetadataPort(Protocol):
    def get_team_mission_graph(self, mission_id: str) -> dict[str, Any]: ...


class TeamCapabilityService:
    """Coordinates capability snapshot persistence with mission metadata.

    Capability tables remain owned by ``TeamCapabilityRepo``. This service owns
    only the cross-aggregate workflow required when a snapshot is pinned to a
    mission.
    """

    def __init__(
        self,
        repository: TeamCapabilityRepo,
        missions: TeamMissionMetadataPort,
        update_mission: Callable[..., dict[str, Any]],
    ) -> None:
        self._repository = repository
        self._missions = missions
        self._update_mission = update_mission

    def get(self, snapshot_id: str) -> dict[str, Any]:
        return self._repository.get_team_capability_snapshot(snapshot_id)

    def get_latest(self, team_id: str) -> dict[str, Any]:
        return self._repository.get_latest_team_capability_snapshot(team_id)

    def resolve(
        self,
        *,
        team_id: str = "",
        source_packet: dict[str, Any] | None = None,
        source_digest_value: str = "",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        return self._repository.resolve_team_capability_snapshot(
            team_id=team_id,
            source_packet=source_packet,
            source_digest_value=source_digest_value,
            force_refresh=force_refresh,
        )

    def upsert(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self._repository.upsert_team_capability_snapshot(snapshot)

    def mark_stale(self, *, snapshot_id: str, reason: str = "") -> dict[str, Any]:
        return self._repository.mark_team_capability_snapshot_stale(
            snapshot_id=snapshot_id,
            reason=reason,
        )

    def bind(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        snapshot_id: str,
    ) -> dict[str, Any]:
        resolved_conversation_id = text(conversation_id)
        if not resolved_conversation_id:
            graph = self._missions.get_team_mission_graph(mission_id)
            mission = graph.get("mission") if isinstance(graph, dict) else {}
            resolved_conversation_id = text((mission or {}).get("conversation_id"))
        binding = self._repository.bind_team_capability_snapshot(
            mission_id=mission_id,
            conversation_id=resolved_conversation_id,
            snapshot_id=snapshot_id,
        )
        self._merge_mission_metadata(mission_id=mission_id, binding=binding)
        return binding

    def get_binding(self, mission_id: str) -> dict[str, Any]:
        return self._repository.get_team_capability_snapshot_binding(mission_id)

    def get_bound(self, mission_id: str) -> dict[str, Any]:
        return self._repository.get_bound_team_capability_snapshot(mission_id)

    def _merge_mission_metadata(
        self,
        *,
        mission_id: str,
        binding: dict[str, Any],
    ) -> None:
        resolved_mission_id = text(mission_id)
        if not resolved_mission_id or not binding:
            return
        graph = self._missions.get_team_mission_graph(resolved_mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else {}
        if not isinstance(mission, dict) or not mission:
            return
        metadata = dict(mission.get("metadata") or {})
        metadata["team_capability_snapshot"] = {
            "snapshot_id": text(binding.get("snapshot_id")),
            "snapshot_version": int(binding.get("snapshot_version") or 0),
            "source_digest": text(binding.get("source_digest")),
            "pinned_at": float(binding.get("pinned_at") or 0),
        }
        self._update_mission(
            mission_id=resolved_mission_id,
            conversation_id=text(mission.get("conversation_id")),
            team_id=text(mission.get("team_id")),
            title=text(mission.get("title")),
            objective=text(mission.get("objective")),
            workspace_id=text(mission.get("workspace_id")),
            workspace_path=text(mission.get("workspace_path")),
            mode=text(mission.get("mode")),
            status=text(mission.get("status")),
            leader_session_id=text(mission.get("leader_session_id")),
            metadata=metadata,
        )


__all__ = ["TeamCapabilityService", "TeamMissionMetadataPort"]
