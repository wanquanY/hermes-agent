"""Conversation participant facade backed by ConversationParticipantRepo."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from hermes_agent.repositories.conversation_participant_repo import (
    ConversationParticipantRepo,
    agent_participant_id,
    leader_participant_id,
    member_participant_id,
    user_participant_id,
)

DEFAULT_USER_ID = "default"
DEFAULT_AGENT_ID = "default"


class ParticipantsMixin:
    def _participant_repo(self) -> ConversationParticipantRepo:
        return ConversationParticipantRepo(self._conn, self._execute_write, self._lock)  # type: ignore[attr-defined]

    def ensure_participant(
        self,
        conversation_session_id: str,
        *,
        participant_id: str,
        role: str,
        member_id: str = "",
        agent_profile_id: str = "",
        agent_profile_version_id: str = "",
        runtime_scope_key: str = "",
        display_name: str = "",
        avatar: str = "",
        metadata_json: str = "",
    ) -> dict:
        return self._participant_repo().ensure_participant(
            conversation_session_id,
            participant_id=participant_id,
            role=role,
            member_id=member_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=agent_profile_version_id,
            runtime_scope_key=runtime_scope_key,
            display_name=display_name,
            avatar=avatar,
            metadata_json=metadata_json,
        )

    def get_participant(self, conversation_session_id: str, participant_id: str) -> Optional[dict]:
        return self._participant_repo().get_participant(conversation_session_id, participant_id)

    def list_conversation_participants(self, conversation_session_id: str) -> List[Dict[str, Any]]:
        return self._participant_repo().list_conversation_participants(conversation_session_id)

    def update_participant_display(
        self,
        conversation_session_id: str,
        participant_id: str,
        *,
        display_name: Optional[str] = None,
        avatar: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> bool:
        return self._participant_repo().update_participant_display(
            conversation_session_id,
            participant_id,
            display_name=display_name,
            avatar=avatar,
            metadata_json=metadata_json,
        )

    def delete_participant(self, conversation_session_id: str, participant_id: str) -> bool:
        return self._participant_repo().delete_participant(conversation_session_id, participant_id)

    def ensure_user_participant(self, conversation_session_id: str, user_id: str = DEFAULT_USER_ID) -> dict:
        return self._participant_repo().ensure_user_participant(conversation_session_id, user_id)

    def ensure_leader_participant(
        self,
        conversation_session_id: str,
        *,
        team_id: str,
        leader_profile_id: str = "",
        display_name: str = "",
        avatar: str = "",
    ) -> dict:
        return self._participant_repo().ensure_leader_participant(
            conversation_session_id,
            team_id=team_id,
            leader_profile_id=leader_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    def ensure_member_participant(
        self,
        conversation_session_id: str,
        *,
        member_id: str,
        agent_profile_id: str = "",
        display_name: str = "",
        avatar: str = "",
    ) -> dict:
        return self._participant_repo().ensure_member_participant(
            conversation_session_id,
            member_id=member_id,
            agent_profile_id=agent_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    def ensure_agent_participant(
        self,
        conversation_session_id: str,
        *,
        agent_profile_id: str,
        display_name: str = "",
        avatar: str = "",
    ) -> dict:
        return self._participant_repo().ensure_agent_participant(
            conversation_session_id,
            agent_profile_id=agent_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    def upsert_conversation_participant(
        self,
        *,
        conversation_session_id: str,
        participant_id: str,
        role: str,
        member_id: str = "",
        agent_profile_id: str = "",
        agent_profile_version_id: str = "",
        runtime_scope_key: str = "",
        display_name: str = "",
        avatar: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._participant_repo().upsert_conversation_participant(
            conversation_session_id=conversation_session_id,
            participant_id=participant_id,
            role=role,
            member_id=member_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=agent_profile_version_id,
            runtime_scope_key=runtime_scope_key,
            display_name=display_name,
            avatar=avatar,
            metadata=metadata,
        )

    def get_conversation_participant(self, conversation_session_id: str, participant_id: str) -> Dict[str, Any]:
        return self._participant_repo().get_conversation_participant(conversation_session_id, participant_id)

    def resolve_participant_id(
        self,
        *,
        conversation_session_id: str,
        agent_profile_id: str = "",
        member_id: str = "",
        runtime_scope_key: str = "",
    ) -> str:
        return self._participant_repo().resolve_participant_id(
            conversation_session_id=conversation_session_id,
            agent_profile_id=agent_profile_id,
            member_id=member_id,
            runtime_scope_key=runtime_scope_key,
        )

    def resolve_participant_id_for_run(
        self,
        conversation_session_id: str,
        *,
        runtime_scope_key: str = "",
        agent_profile_id: str = "",
        member_id: str = "",
    ) -> str:
        return self._participant_repo().resolve_participant_id_for_run(
            conversation_session_id,
            runtime_scope_key=runtime_scope_key,
            agent_profile_id=agent_profile_id,
            member_id=member_id,
        )
