from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *


class TeamMissionContextMixin:
    def _team_mission_memory_context(self, mission: Dict[str, Any]) -> Dict[str, Any]:
        return _memory_state.team_mission_memory_context(self, mission)

    def upsert_team_mission_deliverable(
        self,
        *,
        deliverable_id: str = "",
        mission_id: str,
        node_id: str,
        run_id: str,
        task_id: str = "",
        status: str = "completed",
        result: str = "",
        summary: str = "",
        payload: Dict[str, Any] | None = None,
        artifact_refs: List[Dict[str, Any]] | None = None,
        next_context: Dict[str, Any] | None = None,
        output_contract: Dict[str, Any] | None = None,
        source: str = _deliverable_state.DELIVERABLE_SOURCE_AUTHORITATIVE,
        confidence: float = 0.9,
        visibility: str = _deliverable_state.DELIVERABLE_VISIBILITY_HANDOFF,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        return _deliverable_state.upsert_team_mission_deliverable(
            self,
            deliverable_id=deliverable_id,
            mission_id=mission_id,
            node_id=node_id,
            run_id=run_id,
            task_id=task_id,
            status=status,
            result=result,
            summary=summary,
            payload=payload,
            artifact_refs=artifact_refs,
            next_context=next_context,
            output_contract=output_contract,
            source=source,
            confidence=confidence,
            visibility=visibility,
            created_at=created_at,
            updated_at=updated_at,
        )

    def list_team_mission_deliverables(
        self,
        *,
        mission_id: str = "",
        node_id: str = "",
        run_id: str = "",
        task_id: str = "",
        statuses: List[str] | None = None,
        sources: List[str] | None = None,
        visibility: List[str] | None = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return _deliverable_state.list_team_mission_deliverables(
            self,
            mission_id=mission_id,
            node_id=node_id,
            run_id=run_id,
            task_id=task_id,
            statuses=statuses,
            sources=sources,
            visibility=visibility,
            limit=limit,
        )

    def get_team_mission_deliverable(self, deliverable_id: str) -> Dict[str, Any]:
        return _deliverable_state.get_team_mission_deliverable(self, deliverable_id)

    def latest_team_mission_deliverable_for_run(self, run_id: str) -> Dict[str, Any]:
        return _deliverable_state.latest_team_mission_deliverable_for_run(self, run_id)

    def team_mission_run_has_deliverable(self, run_id: str) -> bool:
        return _deliverable_state.team_mission_run_has_deliverable(self, run_id)

    def upsert_team_mission_result(
        self,
        *,
        result_id: str = "",
        mission_id: str,
        activity_id: str = "",
        status: str,
        outcome: str,
        summary_text: str,
        node_results: List[Dict[str, Any]] | None = None,
        artifact_refs: List[Dict[str, Any]] | None = None,
        leader_report_run_id: str = "",
        leader_report_message_id: str = "",
        metadata: Dict[str, Any] | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        return _result_state.upsert_team_mission_result(
            self,
            result_id=result_id,
            mission_id=mission_id,
            activity_id=activity_id,
            status=status,
            outcome=outcome,
            summary_text=summary_text,
            node_results=node_results,
            artifact_refs=artifact_refs,
            leader_report_run_id=leader_report_run_id,
            leader_report_message_id=leader_report_message_id,
            metadata=metadata,
            created_at=created_at,
            updated_at=updated_at,
        )

    def get_team_mission_result(self, mission_id: str) -> Dict[str, Any]:
        return _result_state.get_team_mission_result(self, mission_id)

    def upsert_team_mission_memory_item(
        self,
        *,
        memory_id: str = "",
        team_id: str,
        mission_id: str,
        conversation_session_id: str,
        task_id: str = "",
        scope: str = "mission_task",
        kind: str = "summary",
        content: str,
        structured_payload: Dict[str, Any] | None = None,
        source_node_ids: List[str] | None = None,
        source_run_ids: List[str] | None = None,
        artifact_refs: List[Dict[str, Any]] | None = None,
        workspace_refs: List[Dict[str, Any]] | None = None,
        confidence: float = 0.75,
        visibility: str = "team",
        status: str = _MEMORY_COMMITTED_STATUS,
        created_at: float | None = None,
        updated_at: float | None = None,
        invalidated_at: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.upsert_team_mission_memory_item(
            self,
            memory_id=memory_id,
            team_id=team_id,
            mission_id=mission_id,
            conversation_session_id=conversation_session_id,
            task_id=task_id,
            scope=scope,
            kind=kind,
            content=content,
            structured_payload=structured_payload,
            source_node_ids=source_node_ids,
            source_run_ids=source_run_ids,
            artifact_refs=artifact_refs,
            workspace_refs=workspace_refs,
            confidence=confidence,
            visibility=visibility,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            invalidated_at=invalidated_at,
        )

    def list_team_mission_memory_items(
        self,
        *,
        mission_id: str = "",
        conversation_session_id: str = "",
        team_id: str = "",
        task_id: str = "",
        kinds: List[str] | None = None,
        statuses: List[str] | None = None,
        visibility: List[str] | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return _memory_state.list_team_mission_memory_items(
            self,
            mission_id=mission_id,
            conversation_session_id=conversation_session_id,
            team_id=team_id,
            task_id=task_id,
            kinds=kinds,
            statuses=statuses,
            visibility=visibility,
            include_deleted=include_deleted,
            limit=limit,
        )

    def update_team_mission_memory_item(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        structured_payload: Dict[str, Any] | None = None,
        visibility: str | None = None,
        status: str | None = None,
        confidence: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.update_team_mission_memory_item(
            self,
            memory_id,
            content=content,
            structured_payload=structured_payload,
            visibility=visibility,
            status=status,
            confidence=confidence,
        )

    def delete_team_mission_memory_item(self, memory_id: str) -> Dict[str, Any]:
        return _memory_state.delete_team_mission_memory_item(self, memory_id)

    def upsert_team_mission_memory_edge(
        self,
        *,
        from_memory_id: str,
        to_memory_id: str = "",
        relation: str,
        metadata: Dict[str, Any] | None = None,
        edge_id: str = "",
        created_at: float | None = None,
    ) -> Dict[str, Any]:
        return _memory_state.upsert_team_mission_memory_edge(
            self,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation=relation,
            metadata=metadata,
            edge_id=edge_id,
            created_at=created_at,
        )

    def list_team_mission_memory_edges(
        self,
        *,
        from_memory_id: str = "",
        to_memory_id: str = "",
        relation: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return _memory_state.list_team_mission_memory_edges(
            self,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation=relation,
            limit=limit,
        )

    def compile_team_mission_memory(
        self,
        *,
        mission_id: str,
        task_id: str = "",
        mode: str = "final",
        source_run_ids: List[str] | None = None,
        emit_event: bool = True,
    ) -> Dict[str, Any]:
        return _memory_state.compile_team_mission_memory(
            self,
            mission_id=mission_id,
            task_id=task_id,
            mode=mode,
            source_run_ids=source_run_ids,
            emit_event=emit_event,
        )

    def build_team_mission_memory_pack(
        self,
        *,
        mission_id: str,
        objective: str = "",
        workspace_id: str = "",
        limit: int = 8,
        include_team_scope: bool = False,
    ) -> Dict[str, Any]:
        return _memory_state.build_team_mission_memory_pack(
            self,
            mission_id=mission_id,
            objective=objective,
            workspace_id=workspace_id,
            limit=limit,
            include_team_scope=include_team_scope,
        )

    def build_team_mission_memory_slice(
        self,
        *,
        mission_id: str,
        node_id: str,
        objective: str = "",
        limit: int = 5,
        include_team_scope: bool = False,
    ) -> Dict[str, Any]:
        return _memory_state.build_team_mission_memory_slice(
            self,
            mission_id=mission_id,
            node_id=node_id,
            objective=objective,
            limit=limit,
            include_team_scope=include_team_scope,
        )
