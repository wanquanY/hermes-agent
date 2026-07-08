"""Session branching facade backed by SessionBranchService."""

from __future__ import annotations

from typing import Any, Dict, Optional

from hermes_agent.domain.session_branch_service import SessionBranchService


class BranchStateMixin:
    def _session_branch_service(self) -> SessionBranchService:
        return SessionBranchService(
            self._conn,  # type: ignore[attr-defined]
            self._execute_write,  # type: ignore[attr-defined]
            self._lock,  # type: ignore[attr-defined]
            self.sanitize_title,  # type: ignore[attr-defined]
        )

    def get_session_branch_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._session_branch_service().get_session_branch_info(session_id)

    def branch_session(
        self,
        *,
        source_session_id: str,
        new_session_id: str,
        branch_point: Optional[Dict[str, Any]] = None,
        scope: str = "through_turn",
        title: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        branch_origin: str = "user_message_action",
    ) -> Dict[str, Any]:
        return self._session_branch_service().branch_session(
            source_session_id=source_session_id,
            new_session_id=new_session_id,
            branch_point=branch_point,
            scope=scope,
            title=title,
            idempotency_key=idempotency_key,
            branch_origin=branch_origin,
        )
