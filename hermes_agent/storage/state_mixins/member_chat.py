"""Member-chat projection facade backed by MemberChatProjectionService."""

from __future__ import annotations

from hermes_agent.domain.member_chat_projection_service import MemberChatProjectionService


class MemberChatStateMixin:
    def _member_chat_projection_service(self) -> MemberChatProjectionService:
        return MemberChatProjectionService(
            self._conn,  # type: ignore[attr-defined]
            self._execute_write,  # type: ignore[attr-defined]
            self.get_messages,  # type: ignore[attr-defined]
            self.append_message,  # type: ignore[attr-defined]
        )

    def recall_member_chat_view_messages(
        self,
        *,
        member_chat_session_id: str,
        source_message_ids: list[int],
    ) -> int:
        return self._member_chat_projection_service().recall_member_chat_view_messages(
            member_chat_session_id=member_chat_session_id,
            source_message_ids=source_message_ids,
        )

    def sync_member_chat_conversation_view(
        self,
        *,
        conversation_session_id: str,
        member_chat_session_id: str,
        member_id: str,
    ) -> int:
        return self._member_chat_projection_service().sync_member_chat_conversation_view(
            conversation_session_id=conversation_session_id,
            member_chat_session_id=member_chat_session_id,
            member_id=member_id,
        )
