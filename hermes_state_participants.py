"""Participant lifecycle rows for conversation identity.

The table is the roster for "who can speak" inside a conversation. This module
owns participant row CRUD plus stable role-based participant ids. Chat paths
may read these helpers, but automatic population is intentionally left to the
caller that owns the conversation lifecycle.
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
    normalized_user_id = _text(user_id)
    if not normalized_user_id or normalized_user_id == DEFAULT_USER_ID:
        return "user"
    return f"user:{normalized_user_id}"


def leader_participant_id(team_id: str) -> str:
    normalized_team_id = _text(team_id)
    if not normalized_team_id:
        raise ValueError("team_id required for leader participant")
    return f"leader:{normalized_team_id}"


def member_participant_id(member_id: str) -> str:
    normalized_member_id = _text(member_id)
    if not normalized_member_id:
        raise ValueError("member_id required for member participant")
    return f"member:{normalized_member_id}"


def agent_participant_id(agent_profile_id: str = "") -> str:
    normalized_profile_id = _text(agent_profile_id)
    if not normalized_profile_id or normalized_profile_id == DEFAULT_AGENT_ID:
        return "agent"
    return f"agent:{normalized_profile_id}"


class ParticipantsMixin:
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
        """Insert or replace a participant row. Returns the row as a dict."""
        normalized_conversation_session_id = _text(conversation_session_id)
        normalized_participant_id = _text(participant_id)
        normalized_role = _text(role)
        if not normalized_conversation_session_id:
            raise ValueError("conversation_session_id required")
        if not normalized_participant_id:
            raise ValueError("participant_id required")
        if not normalized_role:
            raise ValueError("role required")
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                """
                SELECT created_at
                  FROM conversation_participants
                 WHERE conversation_session_id = ? AND participant_id = ?
                """,
                (normalized_conversation_session_id, normalized_participant_id),
            ).fetchone()
            created_at = (
                float(existing["created_at"])
                if existing is not None and "created_at" in existing.keys()
                else now
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO conversation_participants (
                    conversation_session_id, participant_id, role, member_id,
                    agent_profile_id, agent_profile_version_id,
                    runtime_scope_key, display_name, avatar, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_conversation_session_id,
                    normalized_participant_id,
                    normalized_role,
                    _text(member_id),
                    _text(agent_profile_id),
                    _text(agent_profile_version_id),
                    _text(runtime_scope_key),
                    _text(display_name),
                    _text(avatar),
                    _text(metadata_json),
                    created_at,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                  FROM conversation_participants
                 WHERE conversation_session_id = ? AND participant_id = ?
                """,
                (normalized_conversation_session_id, normalized_participant_id),
            ).fetchone()
            return self._participant_row_to_dict(row) if row else {}

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def get_participant(
        self, conversation_session_id: str, participant_id: str
    ) -> Optional[dict]:
        normalized_conversation_session_id = _text(conversation_session_id)
        normalized_participant_id = _text(participant_id)
        if not normalized_conversation_session_id or not normalized_participant_id:
            return None
        try:
            row = self._conn.execute(  # type: ignore[attr-defined]
                """
                SELECT *
                  FROM conversation_participants
                 WHERE conversation_session_id = ? AND participant_id = ?
                """,
                (normalized_conversation_session_id, normalized_participant_id),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return self._participant_row_to_dict(row) if row else None

    def list_conversation_participants(
        self, conversation_session_id: str
    ) -> List[Dict[str, Any]]:
        normalized_conversation_session_id = _text(conversation_session_id)
        if not normalized_conversation_session_id:
            return []
        try:
            rows = self._conn.execute(  # type: ignore[attr-defined]
                """
                SELECT *
                  FROM conversation_participants
                 WHERE conversation_session_id = ?
                 ORDER BY
                    (role = 'user') DESC,
                    (role = 'leader') DESC,
                    created_at ASC,
                    participant_id ASC
                """,
                (normalized_conversation_session_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._participant_row_to_dict(row) for row in rows]

    def update_participant_display(
        self,
        conversation_session_id: str,
        participant_id: str,
        *,
        display_name: Optional[str] = None,
        avatar: Optional[str] = None,
        metadata_json: Optional[str] = None,
    ) -> bool:
        """Patch participant display fields. Only non-None fields are updated."""
        normalized_conversation_session_id = _text(conversation_session_id)
        normalized_participant_id = _text(participant_id)
        if not normalized_conversation_session_id:
            raise ValueError("conversation_session_id required")
        if not normalized_participant_id:
            raise ValueError("participant_id required")
        assignments = ["updated_at = ?"]
        params: List[Any] = [time.time()]
        if display_name is not None:
            assignments.append("display_name = ?")
            params.append(_text(display_name))
        if avatar is not None:
            assignments.append("avatar = ?")
            params.append(_text(avatar))
        if metadata_json is not None:
            assignments.append("metadata_json = ?")
            params.append(_text(metadata_json))
        if len(assignments) == 1:
            return self.get_participant(
                normalized_conversation_session_id, normalized_participant_id
            ) is not None
        params.extend([normalized_conversation_session_id, normalized_participant_id])

        def _do(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                f"""
                UPDATE conversation_participants
                   SET {", ".join(assignments)}
                 WHERE conversation_session_id = ? AND participant_id = ?
                """,
                tuple(params),
            )
            return cursor.rowcount > 0

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def delete_participant(
        self, conversation_session_id: str, participant_id: str
    ) -> bool:
        normalized_conversation_session_id = _text(conversation_session_id)
        normalized_participant_id = _text(participant_id)
        if not normalized_conversation_session_id:
            raise ValueError("conversation_session_id required")
        if not normalized_participant_id:
            raise ValueError("participant_id required")

        def _do(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                """
                DELETE FROM conversation_participants
                 WHERE conversation_session_id = ? AND participant_id = ?
                """,
                (normalized_conversation_session_id, normalized_participant_id),
            )
            return cursor.rowcount > 0

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def ensure_user_participant(
        self, conversation_session_id: str, user_id: str = DEFAULT_USER_ID
    ) -> dict:
        """Ensure the user participant row for a conversation."""
        return self.ensure_participant(
            conversation_session_id,
            participant_id=user_participant_id(user_id),
            role="user",
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
        """Ensure the leader participant row for a team conversation."""
        normalized_team_id = _text(team_id)
        return self.ensure_participant(
            conversation_session_id,
            participant_id=leader_participant_id(normalized_team_id),
            role="leader",
            agent_profile_id=leader_profile_id,
            runtime_scope_key=(
                f"team:{normalized_team_id}:leader-conversation"
                if normalized_team_id
                else ""
            ),
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
        """Ensure a worker member participant row for a team conversation."""
        normalized_member_id = _text(member_id)
        return self.ensure_participant(
            conversation_session_id,
            participant_id=member_participant_id(normalized_member_id),
            role="member",
            member_id=normalized_member_id,
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
        """Ensure the single-agent participant row for a direct conversation."""
        normalized_profile_id = _text(agent_profile_id)
        return self.ensure_participant(
            conversation_session_id,
            participant_id=agent_participant_id(normalized_profile_id),
            role="agent",
            agent_profile_id=normalized_profile_id,
            display_name=display_name,
            avatar=avatar,
        )

    # Backward-compatible write/read names used by existing P0/F5 paths.
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
        normalized_conversation_session_id = _text(conversation_session_id)
        normalized_participant_id = _text(participant_id)
        normalized_role = _text(role)
        if not normalized_conversation_session_id or not normalized_participant_id:
            return {}
        if not normalized_role:
            raise ValueError("role required")
        metadata_json = ""
        if isinstance(metadata, dict) and metadata:
            metadata_json = json.dumps(metadata, ensure_ascii=False)
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT INTO conversation_participants (
                    conversation_session_id, participant_id, role, member_id,
                    agent_profile_id, agent_profile_version_id,
                    runtime_scope_key, display_name, avatar, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_session_id, participant_id) DO UPDATE SET
                    role = CASE WHEN excluded.role != ''
                        THEN excluded.role ELSE conversation_participants.role END,
                    member_id = CASE WHEN excluded.member_id != ''
                        THEN excluded.member_id ELSE conversation_participants.member_id END,
                    agent_profile_id = CASE WHEN excluded.agent_profile_id != ''
                        THEN excluded.agent_profile_id ELSE conversation_participants.agent_profile_id END,
                    agent_profile_version_id = CASE WHEN excluded.agent_profile_version_id != ''
                        THEN excluded.agent_profile_version_id
                        ELSE conversation_participants.agent_profile_version_id END,
                    runtime_scope_key = CASE WHEN excluded.runtime_scope_key != ''
                        THEN excluded.runtime_scope_key
                        ELSE conversation_participants.runtime_scope_key END,
                    display_name = CASE
                        WHEN conversation_participants.display_name = ''
                         AND excluded.display_name != ''
                        THEN excluded.display_name
                        ELSE conversation_participants.display_name END,
                    avatar = CASE
                        WHEN conversation_participants.avatar = ''
                         AND excluded.avatar != ''
                        THEN excluded.avatar
                        ELSE conversation_participants.avatar END,
                    metadata_json = CASE
                        WHEN conversation_participants.metadata_json = ''
                         AND excluded.metadata_json != ''
                        THEN excluded.metadata_json
                        ELSE conversation_participants.metadata_json END,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_conversation_session_id,
                    normalized_participant_id,
                    normalized_role,
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
                """
                SELECT *
                  FROM conversation_participants
                 WHERE conversation_session_id = ? AND participant_id = ?
                """,
                (normalized_conversation_session_id, normalized_participant_id),
            ).fetchone()
            return self._participant_row_to_dict(row) if row else {}

        return self._execute_write(_do)  # type: ignore[attr-defined]

    def get_conversation_participant(
        self, conversation_session_id: str, participant_id: str
    ) -> Dict[str, Any]:
        return self.get_participant(conversation_session_id, participant_id) or {}

    def resolve_participant_id(
        self,
        *,
        conversation_session_id: str,
        agent_profile_id: str = "",
        member_id: str = "",
        runtime_scope_key: str = "",
    ) -> str:
        """Resolve a participant by the authoritative conversation roster."""
        normalized_conversation_session_id = _text(conversation_session_id)
        if not normalized_conversation_session_id:
            return ""
        normalized_member_id = _text(member_id)
        normalized_profile_id = _text(agent_profile_id)
        normalized_scope_key = _text(runtime_scope_key)
        if normalized_member_id:
            return self._conversation_participant_id_for_predicate(
                normalized_conversation_session_id,
                "role IN ('member', 'leader') AND member_id = ?",
                (normalized_member_id,),
            )
        if normalized_profile_id:
            return self._conversation_participant_id_for_predicate(
                normalized_conversation_session_id,
                "agent_profile_id = ?",
                (normalized_profile_id,),
            )
        if normalized_scope_key:
            return self._conversation_participant_id_for_predicate(
                normalized_conversation_session_id,
                "runtime_scope_key = ?",
                (normalized_scope_key,),
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
        return self.resolve_participant_id(
            conversation_session_id=conversation_session_id,
            agent_profile_id=agent_profile_id,
            member_id=member_id,
            runtime_scope_key=runtime_scope_key,
        )

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
                    created_at ASC,
                    participant_id ASC
                 LIMIT 1
                """,
                (conversation_session_id, *values),
            ).fetchone()
        return _text(row["participant_id"] if row else "")

    @staticmethod
    def _participant_row_to_dict(row: sqlite3.Row | None) -> Dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        raw_metadata = _text(item.get("metadata_json"))
        metadata: Dict[str, Any] = {}
        if raw_metadata:
            try:
                parsed = json.loads(raw_metadata)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError):
                metadata = {}
        item["metadata"] = metadata
        return item


SessionDBParticipantMixin = ParticipantsMixin
