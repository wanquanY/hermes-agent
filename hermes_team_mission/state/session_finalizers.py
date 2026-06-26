from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *


class SessionDBTeamMissionFinalizerMixin:
    def reduce_team_mission_graph(self, mission_id: str) -> Dict[str, Any]:
        return _graph_state.reduce_team_mission_graph(self, mission_id)

    def _ensure_team_mission_finalizers(
        self,
        *,
        mission: Dict[str, Any],
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return _graph_state.ensure_team_mission_finalizers(
            self,
            mission=mission,
            nodes=nodes,
            edges=edges,
        )

    def append_team_mission_event_for_run(
        self,
        *,
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        return _event_log.append_team_mission_event_for_run(
            self,
            run_id=run_id,
            event=event,
        )

    def append_team_mission_structural_event(
        self,
        *,
        mission_id: str,
        source_event: Dict[str, Any],
        identity: Dict[str, str] | None = None,
        dedupe_key: str = "",
    ) -> Dict[str, Any]:
        return _event_log.append_team_mission_structural_event(
            self,
            mission_id=mission_id,
            source_event=source_event,
            identity=identity,
            dedupe_key=dedupe_key,
        )

    def append_team_mission_conversation_status_event(
        self,
        *,
        mission_id: str,
        source_event: Dict[str, Any],
        source_mission_seq: int,
    ) -> Dict[str, Any]:
        return _event_log.append_team_mission_conversation_status_event(
            self,
            mission_id=mission_id,
            source_event=source_event,
            source_mission_seq=source_mission_seq,
        )

    def list_team_mission_events(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        return _event_log.list_team_mission_events(
            self,
            mission_id,
            after_seq=after_seq,
            limit=limit,
        )

    def list_team_mission_run_events(
        self,
        mission_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return []
        after_seq = int(after_seq or 0)
        # §§5.1 ABI convergence: the canonical team_mission_events log is the
        # single source of truth for replay. Runtime events are appended to it
        # at write time (append_team_mission_run_event), so replay and live
        # share one monotonic per-mission seq domain. The legacy run_events
        # derived projection (rowid * 1e9 + seq) created a second, incompatible
        # seq domain and has been removed (INV-1 / single source of truth).
        return self.list_team_mission_events(
            mission_id,
            after_seq=after_seq,
            limit=limit,
        )
