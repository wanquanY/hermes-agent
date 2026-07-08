from __future__ import annotations

from typing import Any

from hermes_agent.repositories.agent_profile_repo import AgentProfileRepoImpl


class AgentProfileStateMixin:
    """Compatibility surface for legacy state-facade callers.

    P2 ownership lives in AgentProfileRepoImpl. This mixin intentionally holds
    no SQL and no serialization logic; it delegates the old state-store method
    names to the aggregate repository until the state facade is removed.
    """

    def _agent_profile_repo(self) -> AgentProfileRepoImpl:
        return AgentProfileRepoImpl(self._conn)

    def upsert_agent_profile(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().upsert_agent_profile(**kwargs)

    def get_agent_profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().get_agent_profile(profile_id)

    def get_agent_profile_by_slug(self, slug: str) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().get_agent_profile_by_slug(slug)

    def list_agent_profiles(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            return self._agent_profile_repo().list_agent_profiles(
                include_archived=include_archived
            )

    def archive_agent_profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().archive_agent_profile(profile_id)

    def agent_profile_growth_summary(
        self,
        agent_profile_id: str,
        *,
        agent_profile_version_id: str = "",
        range_preset: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().agent_profile_growth_summary(
                agent_profile_id,
                agent_profile_version_id=agent_profile_version_id,
                range_preset=range_preset,
                start_date=start_date,
                end_date=end_date,
            )

    def upsert_agent_profile_draft(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().upsert_agent_profile_draft(**kwargs)

    def get_agent_profile_draft(self, draft_id: str) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().get_agent_profile_draft(draft_id)

    def list_agent_profile_drafts(
        self,
        *,
        include_published: bool = False,
        include_discarded: bool = False,
        statuses: list[str] | None = None,
        source_session_id: str = "",
        source_agent_profile_id: str = "",
        workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock:
            return self._agent_profile_repo().list_agent_profile_drafts(
                include_published=include_published,
                include_discarded=include_discarded,
                statuses=statuses,
                source_session_id=source_session_id,
                source_agent_profile_id=source_agent_profile_id,
                workspace_id=workspace_id,
            )

    def discard_agent_profile_draft(self, draft_id: str) -> dict[str, Any]:
        with self._lock:
            return self._agent_profile_repo().discard_agent_profile_draft(draft_id)
