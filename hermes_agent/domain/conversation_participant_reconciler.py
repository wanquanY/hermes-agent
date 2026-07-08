"""Conversation participant reconciliation service."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from hermes_agent.domain.participants import (
    agent_participant_id,
    leader_participant_id,
    member_participant_id,
    user_participant_id,
)

CONVERSATION_PARTICIPANTS_BACKFILL_META_KEY = "conversation_participants_backfill_cr_p1_2"


class ConversationParticipantReconciler:
    """Backfill conversation participants from authoritative conversation rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def reconcile(self) -> dict[str, Any]:
        marker = self._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (CONVERSATION_PARTICIPANTS_BACKFILL_META_KEY,),
        ).fetchone()
        participant_rows = int(
            self._conn.execute("SELECT COUNT(*) AS count FROM conversation_participants").fetchone()["count"]
            or 0
        )
        session_rows = int(self._conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"] or 0)
        team_conversation_rows = int(
            self._conn.execute("SELECT COUNT(*) AS count FROM team_mission_conversations").fetchone()["count"]
            or 0
        )
        conversation_estimate = session_rows + team_conversation_rows
        marked = bool(marker and str(marker["value"] or "") == "1")
        if marked and participant_rows >= conversation_estimate:
            return {
                "ran": False,
                "inserted": 0,
                "participants_before": participant_rows,
                "conversation_estimate": conversation_estimate,
            }

        now = time.time()
        inserted = 0
        for row in self._direct_session_rows():
            session_id = str(row["session_id"] or "").strip()
            user_id = str(row["user_id"] or "default").strip()
            agent_profile_id = str(row["agent_profile_id"] or "").strip()
            inserted += self._insert_ignore(
                conversation_session_id=session_id,
                participant_id=user_participant_id(user_id),
                role="user",
                now=now,
            )
            inserted += self._insert_ignore(
                conversation_session_id=session_id,
                participant_id=agent_participant_id(agent_profile_id),
                role="agent",
                agent_profile_id=agent_profile_id,
                agent_profile_version_id=str(row["agent_profile_version_id"] or "").strip(),
                display_name=str(row["display_name"] or "").strip(),
                avatar=str(row["avatar"] or "").strip(),
                now=now,
            )

        for row in self._team_conversation_rows():
            session_id = str(row["session_id"] or "").strip()
            team_id = str(row["team_id"] or "").strip()
            if not session_id:
                continue
            inserted += self._insert_ignore(
                conversation_session_id=session_id,
                participant_id=user_participant_id("default"),
                role="user",
                now=now,
            )
            if not team_id:
                continue
            try:
                leader_id = leader_participant_id(team_id)
            except ValueError:
                leader_id = ""
            inserted += self._insert_ignore(
                conversation_session_id=session_id,
                participant_id=leader_id,
                role="leader",
                agent_profile_id=str(row["leader_profile_id"] or "").strip(),
                runtime_scope_key=f"team:{team_id}:leader-conversation",
                display_name=str(row["leader_name"] or "").strip(),
                avatar=str(row["leader_avatar"] or "").strip(),
                now=now,
            )

        for row in self._team_member_rows():
            role = str(row["role"] or "").strip().lower()
            if role in {"lead", "leader"}:
                continue
            session_id = str(row["session_id"] or "").strip()
            member_id = str(row["member_id"] or "").strip()
            if not session_id or not member_id:
                continue
            inserted += self._insert_ignore(
                conversation_session_id=session_id,
                participant_id=member_participant_id(member_id),
                role="member",
                member_id=member_id,
                agent_profile_id=str(row["agent_profile_id"] or "").strip(),
                agent_profile_version_id=str(row["agent_profile_version_id"] or "").strip(),
                runtime_scope_key=f"member-chat:{str(row['conversation_id'] or '').strip()}:{member_id}",
                display_name=str(row["display_name"] or "").strip(),
                avatar=str(row["avatar"] or "").strip(),
                now=now,
            )

        self._conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'",
            (CONVERSATION_PARTICIPANTS_BACKFILL_META_KEY,),
        )
        return {
            "ran": True,
            "inserted": inserted,
            "participants_before": participant_rows,
            "conversation_estimate": conversation_estimate,
        }

    def _direct_session_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT
                s.id AS session_id,
                COALESCE(NULLIF(s.user_id, ''), 'default') AS user_id,
                COALESCE(si.owner_agent_profile_id, '') AS agent_profile_id,
                COALESCE(si.owner_profile_version_id, '') AS agent_profile_version_id,
                COALESCE(ap.name, '') AS display_name,
                COALESCE(ap.avatar, '') AS avatar
              FROM sessions s
              LEFT JOIN session_index si ON si.session_id = s.id
              LEFT JOIN agent_profiles ap ON ap.id = si.owner_agent_profile_id
            """
        ).fetchall()

    def _team_conversation_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT
                tmc.conversation_session_id AS session_id,
                tmc.team_id AS team_id,
                tmc.conversation_id AS conversation_id,
                COALESCE(at.lead_agent_profile_id, '') AS leader_profile_id,
                COALESCE(ap.name, '') AS leader_name,
                COALESCE(ap.avatar, '') AS leader_avatar
              FROM team_mission_conversations tmc
              LEFT JOIN agent_teams at ON at.id = tmc.team_id
              LEFT JOIN agent_profiles ap ON ap.id = at.lead_agent_profile_id
            """
        ).fetchall()

    def _team_member_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT
                tmc.conversation_session_id AS session_id,
                tmc.conversation_id AS conversation_id,
                m.id AS member_id,
                m.agent_profile_id AS agent_profile_id,
                COALESCE(m.agent_profile_version_id, '') AS agent_profile_version_id,
                COALESCE(NULLIF(m.profile_name, ''), p.name, '') AS display_name,
                COALESCE(NULLIF(m.profile_avatar, ''), p.avatar, '') AS avatar,
                COALESCE(m.role, '') AS role
              FROM team_mission_conversations tmc
              JOIN agent_team_members m ON m.team_id = tmc.team_id
              LEFT JOIN agent_profiles p ON p.id = m.agent_profile_id
             WHERE COALESCE(m.status, '') != 'disabled'
            """
        ).fetchall()

    def _insert_ignore(
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
        now: float,
    ) -> int:
        if not conversation_session_id or not participant_id:
            return 0
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO conversation_participants (
                conversation_session_id, participant_id, role, member_id,
                agent_profile_id, agent_profile_version_id,
                runtime_scope_key, display_name, avatar, metadata_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                conversation_session_id,
                participant_id,
                role,
                member_id,
                agent_profile_id,
                agent_profile_version_id,
                runtime_scope_key,
                display_name,
                avatar,
                now,
                now,
            ),
        )
        return int(cursor.rowcount or 0)


__all__ = [
    "CONVERSATION_PARTICIPANTS_BACKFILL_META_KEY",
    "ConversationParticipantReconciler",
]
