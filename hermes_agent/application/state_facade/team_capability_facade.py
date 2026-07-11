"""Team capability snapshot facade backed by TeamCapabilityRepo."""

from __future__ import annotations

from typing import Any, Dict

from hermes_agent.repositories.team_capability_repo import TeamCapabilityRepo
from hermes_team_capability_snapshot import text as _text


class TeamCapabilityStateMixin:
    def _team_capability_repo(self) -> TeamCapabilityRepo:
        return TeamCapabilityRepo(self._conn, self._execute_write, self._lock)  # type: ignore[attr-defined]

    def get_team_capability_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        return self._team_capability_repo().get_team_capability_snapshot(snapshot_id)

    def get_latest_team_capability_snapshot(self, team_id: str) -> Dict[str, Any]:
        return self._team_capability_repo().get_latest_team_capability_snapshot(team_id)

    def resolve_team_capability_snapshot(
        self,
        *,
        team_id: str = "",
        source_packet: Dict[str, Any] | None = None,
        source_digest_value: str = "",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        return self._team_capability_repo().resolve_team_capability_snapshot(
            team_id=team_id,
            source_packet=source_packet,
            source_digest_value=source_digest_value,
            force_refresh=force_refresh,
        )

    def upsert_team_capability_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return self._team_capability_repo().upsert_team_capability_snapshot(snapshot)

    def mark_team_capability_snapshot_stale(self, *, snapshot_id: str, reason: str = "") -> Dict[str, Any]:
        return self._team_capability_repo().mark_team_capability_snapshot_stale(
            snapshot_id=snapshot_id,
            reason=reason,
        )

    def bind_team_capability_snapshot(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        snapshot_id: str,
    ) -> Dict[str, Any]:
        resolved_conversation_id = _text(conversation_id)
        if not resolved_conversation_id:
            try:
                mission = self.team_mission_graphs.get_team_mission_graph(mission_id).get("mission") or {}  # type: ignore[attr-defined]
                resolved_conversation_id = _text(mission.get("conversation_id"))
            except Exception:
                resolved_conversation_id = ""
        binding = self._team_capability_repo().bind_team_capability_snapshot(
            mission_id=mission_id,
            conversation_id=resolved_conversation_id,
            snapshot_id=snapshot_id,
        )
        self._merge_team_mission_capability_metadata(mission_id=mission_id, binding=binding)
        return binding

    def get_team_capability_snapshot_binding(self, mission_id: str) -> Dict[str, Any]:
        return self._team_capability_repo().get_team_capability_snapshot_binding(mission_id)

    def get_bound_team_capability_snapshot(self, mission_id: str) -> Dict[str, Any]:
        return self._team_capability_repo().get_bound_team_capability_snapshot(mission_id)

    def _merge_team_mission_capability_metadata(self, *, mission_id: str, binding: Dict[str, Any]) -> None:
        mission_id = _text(mission_id)
        if not mission_id or not binding:
            return
        try:
            graph = self.team_mission_graphs.get_team_mission_graph(mission_id)  # type: ignore[attr-defined]
            mission = graph.get("mission") if isinstance(graph, dict) else {}
            if not mission:
                return
            metadata = dict(mission.get("metadata") or {})
            metadata["team_capability_snapshot"] = {
                "snapshot_id": _text(binding.get("snapshot_id")),
                "snapshot_version": int(binding.get("snapshot_version") or 0),
                "source_digest": _text(binding.get("source_digest")),
                "pinned_at": float(binding.get("pinned_at") or 0),
            }
            self.upsert_team_mission(  # type: ignore[attr-defined]
                mission_id=mission_id,
                conversation_id=_text(mission.get("conversation_id")),
                team_id=_text(mission.get("team_id")),
                title=_text(mission.get("title")),
                objective=_text(mission.get("objective")),
                workspace_id=_text(mission.get("workspace_id")),
                workspace_path=_text(mission.get("workspace_path")),
                mode=_text(mission.get("mode")),
                status=_text(mission.get("status")),
                leader_session_id=_text(mission.get("leader_session_id")),
                metadata=metadata,
            )
        except Exception:
            return
