from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *


class TeamMissionFinalizerMixin:
    _warned_deprecated_team_mission_run_events = False

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
        # Audit log only; not for timeline render. Render paths consume
        # run_events for the stored conversation session.
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
        # Deprecated audit alias only; not for timeline render.
        # CR-P4.1: production callers use list_team_mission_events. Keep this
        # warned compatibility alias until P4.3 removes external/test callers.
        if not self._warned_deprecated_team_mission_run_events:
            _log.warning(
                "list_team_mission_run_events is deprecated audit compatibility; "
                "render paths must use ordinary run_events"
            )
            self._warned_deprecated_team_mission_run_events = True
        return self.list_team_mission_events(
            mission_id,
            after_seq=after_seq,
            limit=limit,
        )
