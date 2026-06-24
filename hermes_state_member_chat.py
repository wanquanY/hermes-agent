"""Decoupled group-chat: relay a worker member's direct-chat reply into the team
conversation session.

This is intentionally INDEPENDENT of the team-mission/task machinery. A member
@-chat is NOT a mission node — it has no graph node, no mission run-binding, and
is never touched by the terminal-mission run reaper. The member runs on its own
session (isolated worker); its reply is relayed here into the shared conversation
session, tagged with the member's identity, so the leader + every member share
one conversation transcript.

Mirrors the team-mission conversation-mirror's shape (append a run event to the
conversation session + persist the assistant message) but keyed by a member-chat
run registry instead of a mission binding.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict


def _text(value: Any) -> str:
    return str(value or "").strip()


def _complete_text(payload: Dict[str, Any]) -> str:
    for key in ("text", "final_response", "finalResponse", "summary", "message"):
        value = payload.get(key)
        if _text(value):
            return _text(value)
    return ""


class SessionDBMemberChatMixin:
    # ── registry ─────────────────────────────────────────────────────
    def register_member_chat_run(
        self,
        *,
        run_id: str,
        conversation_session_id: str,
        member_id: str,
        agent_profile_id: str = "",
        display_name: str = "",
    ) -> None:
        run_id = _text(run_id)
        if not run_id:
            return

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO member_chat_runs "
                "(run_id, conversation_session_id, member_id, agent_profile_id, display_name, relayed, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "conversation_session_id=excluded.conversation_session_id, "
                "member_id=excluded.member_id, "
                "agent_profile_id=excluded.agent_profile_id, "
                "display_name=excluded.display_name",
                (
                    run_id,
                    _text(conversation_session_id),
                    _text(member_id),
                    _text(agent_profile_id),
                    _text(display_name),
                    time.time(),
                ),
            )

        self._execute_write(_do)

    def get_member_chat_run(self, run_id: str) -> Dict[str, Any]:
        run_id = _text(run_id)
        if not run_id:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, conversation_session_id, member_id, agent_profile_id, display_name, relayed "
                "FROM member_chat_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return {}
        keys = ["run_id", "conversation_session_id", "member_id", "agent_profile_id", "display_name", "relayed"]
        if isinstance(row, sqlite3.Row):
            return {k: row[k] for k in keys}
        return {k: row[i] for i, k in enumerate(keys)}

    def _mark_member_chat_run_relayed(self, run_id: str) -> None:
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE member_chat_runs SET relayed = 1 WHERE run_id = ?", (_text(run_id),))

        self._execute_write(_do)

    # ── relay projection (called from the run-event write hook) ───────
    def _project_member_chat_run_event(self, *, run_id: str, saved: Dict[str, Any]) -> None:
        """If ``run_id`` is a registered member-chat run, relay its completed
        reply into the conversation session with member identity. First cut:
        only message.complete (the final reply), so the reply shows live + is
        persisted for the leader to read. Streaming deltas can be added later."""
        if not run_id or not isinstance(saved, dict):
            return
        if _text(saved.get("type")) != "message.complete":
            return
        registration = self.get_member_chat_run(run_id)
        if not registration or int(registration.get("relayed") or 0):
            return
        conversation_session_id = _text(registration.get("conversation_session_id"))
        source_session_id = _text(saved.get("stored_session_id") or saved.get("session_id"))
        if not conversation_session_id or conversation_session_id == source_session_id:
            return
        payload = dict(saved.get("payload") or {}) if isinstance(saved.get("payload"), dict) else {}
        reply_text = _complete_text(payload)
        if not reply_text:
            return

        member_identity = {
            "kind": "member_chat",
            "surface": "member_chat",
            "member_id": _text(registration.get("member_id")),
            "agent_profile_id": _text(registration.get("agent_profile_id")),
            "display_name": _text(registration.get("display_name")),
            "conversation_session_id": conversation_session_id,
        }
        relay_run_id = f"member-chat:{run_id}"
        relay_payload = {
            **payload,
            "text": reply_text,
            "run_id": relay_run_id,
            "source_run_id": run_id,
            "source_session_id": source_session_id,
            "team_mission": member_identity,
            "member_chat_conversation_mirror": True,
        }
        relay_event = {
            **{k: v for k, v in saved.items() if k not in ("payload", "seq")},
            "stored_session_id": conversation_session_id,
            "session_id": conversation_session_id,
            "run_id": relay_run_id,
            "runtime_scope_key": f"member-chat:{_text(registration.get('member_id'))}",
            "payload": relay_payload,
            "seq": 0,
            "timestamp": float(saved.get("timestamp") or time.time()),
        }

        previous = getattr(self, "_member_chat_projecting", False)
        self._member_chat_projecting = True
        try:
            try:
                if not self.get_session(conversation_session_id):
                    self.create_session(conversation_session_id, source="team_mission", transient=False)
            except Exception:
                pass
            try:
                self.append_message(
                    conversation_session_id,
                    role="assistant",
                    content=reply_text,
                    metadata={"team_mission": member_identity},
                )
            except Exception:
                pass
            self.append_run_event(conversation_session_id, relay_event)
            self._mark_member_chat_run_relayed(run_id)
        finally:
            self._member_chat_projecting = previous
