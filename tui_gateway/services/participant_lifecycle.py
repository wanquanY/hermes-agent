"""Worker-side facade for conversation participant lifecycle DB calls.

This module deliberately does not auto-populate participants. It gives worker
code a static, auditable surface for the participant lifecycle
helpers exposed through the worker DB proxy.
"""

from __future__ import annotations

from typing import Any, Optional


class ParticipantLifecycleStore:
    def __init__(self, db: Any) -> None:
        self._db = db

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
        return self._db.ensure_participant(
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

    def ensure_user_participant(
        self, conversation_session_id: str, user_id: str = "default"
    ) -> dict:
        return self._db.ensure_user_participant(
            conversation_session_id,
            user_id=user_id,
        )

    def ensure_leader_participant(
        self,
        conversation_session_id: str,
        *,
        team_id: str,
        leader_profile_id: str = "",
        display_name: str = "",
        avatar: str = "",
    ) -> dict:
        return self._db.ensure_leader_participant(
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
        return self._db.ensure_member_participant(
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
        return self._db.ensure_agent_participant(
            conversation_session_id,
            agent_profile_id=agent_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    def get_participant(
        self, conversation_session_id: str, participant_id: str
    ) -> Optional[dict]:
        return self._db.get_participant(conversation_session_id, participant_id)

    def update_participant_display(
        self,
        conversation_session_id: str,
        participant_id: str,
        *,
        display_name: Optional[str] = None,
        avatar: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> bool:
        return self._db.update_participant_display(
            conversation_session_id,
            participant_id,
            display_name=display_name,
            avatar=avatar,
            metadata_json=metadata_json,
        )

    def delete_participant(
        self, conversation_session_id: str, participant_id: str
    ) -> bool:
        return self._db.delete_participant(conversation_session_id, participant_id)
