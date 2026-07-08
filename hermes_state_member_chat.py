"""Team conversation participant-view projection.

A member @-chat is NOT a mission node. P2 RunContext routes user-visible
worker events directly to the team conversation session.

This module owns:
  - the participant-view projection (how the team conversation's shared
    message log looks from a given participant's first-person perspective:
    that participant's own assistant turns stay assistant, every other
    speaker becomes user-side observed speech with a "[<speaker>] ..."
    prefix so the LLM doesn't impersonate them)
"""

from __future__ import annotations

import sqlite3
import json
from typing import Any, Dict

from hermes_team_mission.domain.member_chat_projection import LEADER_PARTICIPANT_ID
from hermes_team_mission.domain.member_chat_projection import coerce_message_metadata
from hermes_team_mission.domain.member_chat_projection import project_message_for_viewer
from hermes_team_mission.domain.member_chat_projection import project_messages_for_viewer


def _text(value: Any) -> str:
    return str(value or "").strip()


class MemberChatStateMixin:
    def recall_member_chat_view_messages(
        self,
        *,
        member_chat_session_id: str,
        source_message_ids: "list[int]",
    ) -> int:
        """Deactivate (active=0) any view rows in a member-chat session whose
        ``metadata.member_chat_view.source_message_id`` points at one of the
        recalled conversation messages. Mirrors the recall semantics into the
        worker's hydration view so the next turn the member runs no longer
        sees the retracted turn.

        Returns the number of rows deactivated.
        """
        member_chat_session_id = _text(member_chat_session_id)
        if not member_chat_session_id or not source_message_ids:
            return 0
        # source_message_id is stored stringified inside metadata_json; match
        # the exact JSON fragment to avoid LIKE false positives.
        normalized_ids = [str(int(mid)) for mid in source_message_ids if str(mid).strip()]
        if not normalized_ids:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            affected = 0
            for mid in normalized_ids:
                fragment = f'"source_message_id": "{mid}"'
                alt_fragment = f'"source_message_id":"{mid}"'  # no-space variant
                cur = conn.execute(
                    "UPDATE messages SET active = 0 "
                    "WHERE session_id = ? AND active = 1 "
                    "  AND (instr(metadata_json, ?) > 0 OR instr(metadata_json, ?) > 0)",
                    (member_chat_session_id, fragment, alt_fragment),
                )
                affected += int(cur.rowcount or 0)
            return affected

        return int(self._execute_write(_do) or 0)

    # ── group-chat history view ──────────────────────────────────────
    def sync_member_chat_conversation_view(
        self,
        *,
        conversation_session_id: str,
        member_chat_session_id: str,
        member_id: str,
    ) -> int:
        """Materialize the team conversation's structured history into the
        member-chat session's messages table.

        The worker's runtime hydrates conversation history from
        ``messages WHERE session_id = <member-chat-session>``, so this is how
        we hand it a REAL chat history (system prompt + role-tagged turns)
        instead of stringly cramming a transcript into the user prompt — the
        latter makes the LLM copy other assistants' wording ("I am Hermes
        Agent...") because to it those just look like prior turns from the
        same assistant role.

        Role remap (from this member's first-person perspective):
          - source user messages → ``role=user``, content unchanged
          - this member's OWN past replies in the conversation
            → ``role=assistant``
          - everyone else (leader, other members, unattributed assistants)
            → ``role=user`` with a ``[<speaker> 在群聊里说] <content>``
            prefix, so the LLM treats them as observed group-chat speech
            from other participants, not its own prior turns to mimic

        Skip rules:
          - tool messages / empty content
          - source messages already materialized (tracked by source message id
            stamped in metadata.member_chat_view.source_message_id)
          - this member's OWN ``kind=member_chat`` rows AND
            ``kind=member_chat_user`` current request — the worker already
            has those locally (its own past assistant turns and the in-
            flight user prompt arrive through `text=` on run.submit).

        Idempotent — only newly-arrived source messages get appended.
        Returns the number of rows appended this call.
        """
        conversation_session_id = _text(conversation_session_id)
        member_chat_session_id = _text(member_chat_session_id)
        member_id = _text(member_id)
        if not conversation_session_id or not member_chat_session_id:
            return 0

        source_messages = self.get_messages(conversation_session_id) or []
        own_messages = self.get_messages(member_chat_session_id) or []

        mirrored_source_ids: set[str] = set()
        for m in own_messages:
            if not isinstance(m, dict):
                continue
            meta = coerce_message_metadata(m.get("metadata"))
            view = meta.get("member_chat_view") if isinstance(meta.get("member_chat_view"), dict) else {}
            source_id = _text(view.get("source_message_id"))
            if source_id:
                mirrored_source_ids.add(source_id)

        appended = 0
        for src in source_messages:
            if not isinstance(src, dict):
                continue
            source_id = _text(src.get("id"))
            if not source_id or source_id in mirrored_source_ids:
                continue
            projected = project_message_for_viewer(src, member_id)
            if projected is None:
                continue
            try:
                self.append_message(
                    member_chat_session_id,
                    role=projected["role"],
                    content=projected["content"],
                    metadata={"member_chat_view": {"source_message_id": source_id}},
                )
                appended += 1
            except Exception:
                continue
        return appended
