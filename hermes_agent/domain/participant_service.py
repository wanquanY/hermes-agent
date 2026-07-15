"""Application boundary for conversation participant lifecycle and repair."""

from __future__ import annotations

from typing import Any

from hermes_agent.domain.conversation_participant_reconciler import (
    ConversationParticipantReconciler,
)
from hermes_agent.repositories.conversation_participant_repo import (
    ConversationParticipantRepo,
)
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class ParticipantService:
    def __init__(
        self,
        repository: ConversationParticipantRepo,
        unit_of_work: SqliteUnitOfWork,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work

    def reconcile(self) -> dict[str, Any]:
        return self._unit_of_work.execute(
            lambda conn: ConversationParticipantReconciler(conn).reconcile()
        )

    def ensure_participant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.ensure_participant(*args, **kwargs)

    def ensure_user_participant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.ensure_user_participant(*args, **kwargs)

    def ensure_leader_participant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.ensure_leader_participant(*args, **kwargs)

    def ensure_member_participant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.ensure_member_participant(*args, **kwargs)

    def ensure_agent_participant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.ensure_agent_participant(*args, **kwargs)

    def get_participant(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self._repository.get_participant(*args, **kwargs)

    def list_conversation_participants(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self._repository.list_conversation_participants(*args, **kwargs)

    def update_participant_display(self, *args: Any, **kwargs: Any) -> bool:
        return self._repository.update_participant_display(*args, **kwargs)

    def delete_participant(self, *args: Any, **kwargs: Any) -> bool:
        return self._repository.delete_participant(*args, **kwargs)

    def upsert_conversation_participant(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._repository.upsert_conversation_participant(*args, **kwargs)

    def get_conversation_participant(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._repository.get_conversation_participant(*args, **kwargs)

    def resolve_participant_id(self, *args: Any, **kwargs: Any) -> str:
        return self._repository.resolve_participant_id(*args, **kwargs)

    def resolve_participant_id_for_run(self, *args: Any, **kwargs: Any) -> str:
        return self._repository.resolve_participant_id_for_run(*args, **kwargs)

    def advance_actor_state(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.advance_actor_state(*args, **kwargs)


__all__ = ["ParticipantService"]
