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


# Stable, well-known participant ids for the non-member roles. Member
# participants use their team member_id as the participant_id so the identity
# survives across missions and the @-mention path can address them directly.
PARTICIPANT_USER = "user"
PARTICIPANT_LEADER = "leader"


class SessionDBParticipantMixin:
    # ── write ────────────────────────────────────────────────────────
    def upsert_conversation_participant(
        self,
        *,
        conversation_session_id: str,
        participant_id: str,
        role: str = "member",
        member_id: str = "",
        agent_profile_id: str = "",
        agent_profile_version_id: str = "",
        runtime_scope_key: str = "",
        display_name: str = "",
        avatar: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        conversation_session_id = _text(conversation_session_id)
        participant_id = _text(participant_id)
        if not conversation_session_id or not participant_id:
            return
        metadata_json = ""
        if isinstance(metadata, dict) and metadata:
            try:
                metadata_json = json.dumps(metadata, ensure_ascii=False)
            except (TypeError, ValueError):
                metadata_json = ""

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO conversation_participants "
                "(conversation_session_id, participant_id, role, member_id, "
                " agent_profile_id, agent_profile_version_id, runtime_scope_key, "
                " display_name, avatar, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_session_id, participant_id) DO UPDATE SET "
                "  role=excluded.role, "
                "  member_id=excluded.member_id, "
                "  agent_profile_id=excluded.agent_profile_id, "
                "  agent_profile_version_id=excluded.agent_profile_version_id, "
                "  runtime_scope_key=excluded.runtime_scope_key, "
                "  display_name=CASE WHEN excluded.display_name != '' "
                "                    THEN excluded.display_name ELSE "
                "                    conversation_participants.display_name END, "
                "  avatar=CASE WHEN excluded.avatar != '' THEN excluded.avatar "
                "              ELSE conversation_participants.avatar END, "
                "  metadata_json=CASE WHEN excluded.metadata_json != '' "
                "                     THEN excluded.metadata_json ELSE "
                "                     conversation_participants.metadata_json END, "
                "  updated_at=excluded.updated_at",
                (
                    conversation_session_id,
                    participant_id,
                    _text(role) or "member",
                    _text(member_id),
                    _text(agent_profile_id),
                    _text(agent_profile_version_id),
                    _text(runtime_scope_key),
                    _text(display_name),
                    _text(avatar),
                    metadata_json,
                    time.time(),
                    time.time(),
                ),
            )

        self._execute_write(_do)  # type: ignore[attr-defined]

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
    def resolve_participant_id_for_run(
        self,
        conversation_session_id: str,
        *,
        runtime_scope_key: str = "",
        agent_profile_id: str = "",
        member_id: str = "",
    ) -> str:
        """Map a run's identity hints to a participant_id within a conversation.

        Priority, most-specific first: explicit member_id, then the runtime
        scope key (uniquely identifies a running participant), then the agent
        profile id (UNIQUE(team_id, agent_profile_id) makes this unambiguous
        within a team). Returns '' when nothing matches — the caller decides
        the fallback (leader for team rooms, the lone agent for 1:1).
        """
        conversation_session_id = _text(conversation_session_id)
        if not conversation_session_id:
            return ""
        member_id = _text(member_id)
        runtime_scope_key = _text(runtime_scope_key)
        agent_profile_id = _text(agent_profile_id)
        participants = self.list_conversation_participants(conversation_session_id)
        if not participants:
            return ""
        if member_id:
            for p in participants:
                if p.get("member_id") and p["member_id"] == member_id:
                    return p["participant_id"]
            # member_id may itself be the participant_id
            for p in participants:
                if p["participant_id"] == member_id:
                    return p["participant_id"]
        if runtime_scope_key:
            for p in participants:
                if p.get("runtime_scope_key") and p["runtime_scope_key"] == runtime_scope_key:
                    return p["participant_id"]
        if agent_profile_id:
            for p in participants:
                if p.get("agent_profile_id") and p["agent_profile_id"] == agent_profile_id:
                    return p["participant_id"]
        return ""

    # ── helpers ──────────────────────────────────────────────────────
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
