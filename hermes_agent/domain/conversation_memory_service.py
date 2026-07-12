"""Application boundary for conversation memory and actor context state."""

from __future__ import annotations

from typing import Any

from hermes_agent.repositories.conversation_memory_repo import ConversationMemoryRepo
from hermes_agent.domain.conversation_memory import memory_conflict_sets


class ConversationMemoryService:
    def __init__(self, repository: ConversationMemoryRepo) -> None:
        self._repository = repository

    def create_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.create_item(*args, **kwargs)

    def current_conversation_revision(self, *args: Any, **kwargs: Any) -> int:
        return self._repository.current_conversation_revision(*args, **kwargs)

    def get_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.get_item(*args, **kwargs)

    def list_items(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._repository.list_items(*args, **kwargs)

    def update_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.update_item(*args, **kwargs)

    def upsert_edge(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.upsert_edge(*args, **kwargs)

    def list_edges(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._repository.list_edges(*args, **kwargs)

    def transition_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.transition_item(*args, **kwargs)

    def list_visible(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._repository.list_visible(*args, **kwargs)

    def resolve_visible(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        items = self._repository.list_visible(*args, **kwargs)
        return {"items": items, "conflicts": memory_conflict_sets(items)}

    def create_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.create_snapshot(*args, **kwargs)

    def latest_actor_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.latest_actor_summary(*args, **kwargs)

    def actor_compaction_state(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        return self._repository.actor_compaction_state(*args, **kwargs)

    def create_activity_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.create_activity_snapshot(*args, **kwargs)

    def latest_activity_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.latest_activity_snapshot(*args, **kwargs)

    def latest_activity_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.latest_activity_summary(*args, **kwargs)

    def latest_activity_summary_revision(self, *args: Any, **kwargs: Any) -> int:
        return self._repository.latest_activity_summary_revision(*args, **kwargs)

    def latest_node_attempt_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.latest_node_attempt_summary(*args, **kwargs)

    def latest_node_summary_revision(self, *args: Any, **kwargs: Any) -> int:
        return self._repository.latest_node_summary_revision(*args, **kwargs)

    def commit_actor_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.commit_actor_summary(*args, **kwargs)

    def commit_activity_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.commit_activity_summary(*args, **kwargs)

    def commit_node_attempt_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._repository.commit_node_attempt_summary(*args, **kwargs)

    def try_acquire_compaction_lease(self, *args: Any, **kwargs: Any) -> str:
        return self._repository.try_acquire_compaction_lease(*args, **kwargs)

    def release_compaction_lease(self, *args: Any, **kwargs: Any) -> bool:
        return self._repository.release_compaction_lease(*args, **kwargs)

    def invalidate_summaries_for_events(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[str]:
        return self._repository.invalidate_summaries_for_events(*args, **kwargs)


__all__ = ["ConversationMemoryService"]
