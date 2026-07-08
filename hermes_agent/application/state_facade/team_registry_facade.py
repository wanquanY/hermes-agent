"""Agent team registry facade backed by TeamRegistryRepo."""

from __future__ import annotations

from typing import Any, Dict, List

from hermes_agent.repositories.team_registry_repo import TeamRegistryRepo


class TeamRegistryStateMixin:
    def _team_registry_repo(self) -> TeamRegistryRepo:
        return TeamRegistryRepo(self._conn, self._execute_write, self._lock)  # type: ignore[attr-defined]

    def upsert_agent_team(
        self,
        *,
        team_id: str,
        name: str,
        avatar: Any = None,
        description: str = "",
        lead_agent_profile_id: str = "",
        default_mode: str = "supervised_mission",
        policy: Dict[str, Any] | None = None,
        status: str = "active",
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        return self._team_registry_repo().upsert_agent_team(
            team_id=team_id,
            name=name,
            avatar=avatar,
            description=description,
            lead_agent_profile_id=lead_agent_profile_id,
            default_mode=default_mode,
            policy=policy,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
        )

    def get_agent_team(self, team_id: str) -> Dict[str, Any]:
        return self._team_registry_repo().get_agent_team(team_id)

    def list_agent_teams(self, *, include_archived: bool = False) -> List[Dict[str, Any]]:
        return self._team_registry_repo().list_agent_teams(include_archived=include_archived)

    def list_agent_team_summaries(self, *, include_archived: bool = False) -> List[Dict[str, Any]]:
        return self._team_registry_repo().list_agent_team_summaries(include_archived=include_archived)

    def archive_agent_team(self, team_id: str) -> Dict[str, Any]:
        return self._team_registry_repo().archive_agent_team(team_id)

    def upsert_agent_team_member(
        self,
        *,
        member_id: str,
        team_id: str,
        agent_profile_id: str,
        agent_profile_version_id: str = "",
        role: str = "member",
        capability_tags: List[str] | None = None,
        auto_assignable: bool = True,
        max_concurrent_nodes: int = 1,
        permission_mode: str = "inherit_profile",
        status: str = "active",
        profile_name: str = "",
        profile_avatar: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        return self._team_registry_repo().upsert_agent_team_member(
            member_id=member_id,
            team_id=team_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=agent_profile_version_id,
            role=role,
            capability_tags=capability_tags,
            auto_assignable=auto_assignable,
            max_concurrent_nodes=max_concurrent_nodes,
            permission_mode=permission_mode,
            status=status,
            profile_name=profile_name,
            profile_avatar=profile_avatar,
            created_at=created_at,
            updated_at=updated_at,
        )

    def get_agent_team_member(self, member_id: str) -> Dict[str, Any]:
        return self._team_registry_repo().get_agent_team_member(member_id)

    def list_agent_team_members(self, team_id: str) -> List[Dict[str, Any]]:
        return self._team_registry_repo().list_agent_team_members(team_id)

    def delete_agent_team_member(self, member_id: str) -> Dict[str, Any]:
        return self._team_registry_repo().delete_agent_team_member(member_id)

    def get_agent_team_with_members(self, team_id: str) -> Dict[str, Any]:
        return self._team_registry_repo().get_agent_team_with_members(team_id)
