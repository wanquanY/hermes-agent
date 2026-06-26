"""Conversation participants — the first-class "who is speaking" entity.

Part of the conversation-architecture refactor (docs/Hermes/V0.9.5/
conversation-architecture-redesign.md, P1). A conversation is a room; a
participant is a stable identity inside that room (the user, the leader agent,
each worker member, or — for a degenerate 1:1 chat — the single agent profile).

Every recorded event will eventually be stamped with the participant_id of
whoever produced it, replacing the brittle four-path nodeId→node→member→profile
speaker lookup the frontend does today. This module owns the participant table
and the run→participant resolver that the publish path uses to stamp events.

Purely additive: populating this table and reading it changes no existing
behavior on its own. Stamping at publish + frontend consumption land in later
P1 chunks.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


DEFAULT_USER_ID = "default"
DEFAULT_AGENT_ID = "default"


def user_participant_id(user_id: str = "") -> str:
    return f"user:{_text(user_id) or DEFAULT_USER_ID}"


def leader_participant_id(conversation_id: str) -> str:
    conversation_id = _text(conversation_id)
    if not conversation_id:
        raise ValueError("conversation_id required for leader participant")
    return f"leader:{conversation_id}"


def member_participant_id(member_id: str) -> str:
    member_id = _text(member_id)
    if not member_id:
        raise ValueError("member_id required for member participant")
    return f"member:{member_id}"


def agent_participant_id(agent_profile_id: str = "") -> str:
    return f"agent:{_text(agent_profile_id) or DEFAULT_AGENT_ID}"


class SessionDBParticipantMixin:
    # ── write ────────────────────────────────────────────────────────
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
        conversation_session_id = _text(conversation_session_id)
        participant_id = _text(participant_id)
        role = _text(role)
        if not conversation_session_id or not participant_id:
            return {}
        if not role:
            raise ValueError("role required")
        metadata_json = ""
        if isinstance(metadata, dict) and metadata:
            metadata_json = json.dumps(metadata, ensure_ascii=False)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                "INSERT INTO conversation_participants "
                "(conversation_session_id, participant_id, role, member_id, "
                " agent_profile_id, agent_profile_version_id, runtime_scope_key, "
                " display_name, avatar, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_session_id, participant_id) DO UPDATE SET "
                "  role=CASE WHEN excluded.role != '' THEN excluded.role "
                "            ELSE conversation_participants.role END, "
                "  member_id=CASE WHEN excluded.member_id != '' THEN excluded.member_id "
                "                 ELSE conversation_participants.member_id END, "
                "  agent_profile_id=CASE WHEN excluded.agent_profile_id != '' THEN excluded.agent_profile_id "
                "                        ELSE conversation_participants.agent_profile_id END, "
                "  agent_profile_version_id=CASE WHEN excluded.agent_profile_version_id != '' "
                "                                THEN excluded.agent_profile_version_id "
                "                                ELSE conversation_participants.agent_profile_version_id END, "
                "  runtime_scope_key=CASE WHEN excluded.runtime_scope_key != '' THEN excluded.runtime_scope_key "
                "                         ELSE conversation_participants.runtime_scope_key END, "
                "  display_name=CASE WHEN conversation_participants.display_name = '' "
                "                         AND excluded.display_name != '' "
                "                    THEN excluded.display_name "
                "                    ELSE conversation_participants.display_name END, "
                "  avatar=CASE WHEN conversation_participants.avatar = '' "
                "                   AND excluded.avatar != '' "
                "              THEN excluded.avatar ELSE conversation_participants.avatar END, "
                "  metadata_json=CASE WHEN conversation_participants.metadata_json = '' "
                "                         AND excluded.metadata_json != '' "
                "                    THEN excluded.metadata_json "
                "                    ELSE conversation_participants.metadata_json END, "
                "  updated_at=excluded.updated_at",
                (
                    conversation_session_id,
                    participant_id,
                    role,
                    _text(member_id),
                    _text(agent_profile_id),
                    _text(agent_profile_version_id),
                    _text(runtime_scope_key),
                    _text(display_name),
                    _text(avatar),
                    metadata_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM conversation_participants "
                "WHERE conversation_session_id = ? AND participant_id = ?",
                (conversation_session_id, participant_id),
            ).fetchone()
            return self._participant_row_to_dict(row) if row else {}

        return self._execute_write(_do)  # type: ignore[attr-defined]

    # ── read ─────────────────────────────────────────────────────────
    def list_conversation_participants(
        self, conversation_session_id: str
    ) -> List[Dict[str, Any]]:
        conversation_session_id = _text(conversation_session_id)
        if not conversation_session_id:
            return []
        try:
            rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM conversation_participants "
                "WHERE conversation_session_id = ? "
                "ORDER BY (role = 'user') DESC, (role = 'leader') DESC, created_at ASC",
                (conversation_session_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._participant_row_to_dict(r) for r in rows]

    def get_conversation_participant(
        self, conversation_session_id: str, participant_id: str
    ) -> Dict[str, Any]:
        conversation_session_id = _text(conversation_session_id)
        participant_id = _text(participant_id)
        if not conversation_session_id or not participant_id:
            return {}
        try:
            row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM conversation_participants "
                "WHERE conversation_session_id = ? AND participant_id = ?",
                (conversation_session_id, participant_id),
            ).fetchone()
        except sqlite3.OperationalError:
            return {}
        return self._participant_row_to_dict(row) if row else {}

    # ── resolver (keystone: run → participant) ───────────────────────
    def resolve_participant_id(
        self,
        *,
        conversation_session_id: str,
        agent_profile_id: str = "",
        member_id: str = "",
        runtime_scope_key: str = "",
    ) -> str:
        """Resolve a participant by the authoritative conversation roster.

        Empty string is a legitimate lookup miss. Callers must keep legacy
        speaker fallback available and must not silently substitute leader.
        """
        conversation_session_id = _text(conversation_session_id)
        if not conversation_session_id:
            return ""
        member_id = _text(member_id)
        agent_profile_id = _text(agent_profile_id)
        runtime_scope_key = _text(runtime_scope_key)
        if member_id:
            return self._conversation_participant_id_for_predicate(
                conversation_session_id,
                "role IN ('member', 'leader') AND member_id = ?",
                (member_id,),
            )
        if agent_profile_id:
            return self._conversation_participant_id_for_predicate(
                conversation_session_id,
                "agent_profile_id = ?",
                (agent_profile_id,),
            )
        if runtime_scope_key:
            return self._conversation_participant_id_for_predicate(
                conversation_session_id,
                "runtime_scope_key = ?",
                (runtime_scope_key,),
            )
        return ""

    def resolve_participant_id_for_run(
        self,
        conversation_session_id: str,
        *,
        runtime_scope_key: str = "",
        agent_profile_id: str = "",
        member_id: str = "",
    ) -> str:
        """Map a run's identity hints to a participant_id within a conversation.

        Kept for older callers; PR-B's authoritative lookup is
        ``resolve_participant_id``.
        """
        return self.resolve_participant_id(
            conversation_session_id=conversation_session_id,
            agent_profile_id=agent_profile_id,
            member_id=member_id,
            runtime_scope_key=runtime_scope_key,
        )

    # ── helpers ──────────────────────────────────────────────────────
    def _conversation_participant_id_for_predicate(
        self,
        conversation_session_id: str,
        where_sql: str,
        values: tuple[str, ...],
    ) -> str:
        with self._lock:  # type: ignore[attr-defined]
            row = self._conn.execute(  # type: ignore[attr-defined]
                f"""
                SELECT participant_id
                FROM conversation_participants
                WHERE conversation_session_id = ?
                  AND {where_sql}
                ORDER BY
                    role = 'member' DESC,
                    role = 'leader' DESC,
                    role = 'agent' DESC,
                    role = 'user' DESC,
                    created_at ASC
                LIMIT 1
                """,
                (conversation_session_id, *values),
            ).fetchone()
        return _text(row["participant_id"] if row else "")

    @staticmethod
    def _participant_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        raw = row["metadata_json"] if "metadata_json" in row.keys() else ""
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError):
                metadata = {}
        return {
            "conversation_session_id": row["conversation_session_id"],
            "participant_id": row["participant_id"],
            "role": row["role"],
            "member_id": row["member_id"],
            "agent_profile_id": row["agent_profile_id"],
            "agent_profile_version_id": row["agent_profile_version_id"],
            "runtime_scope_key": row["runtime_scope_key"],
            "display_name": row["display_name"],
            "avatar": row["avatar"],
            "metadata": metadata,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
