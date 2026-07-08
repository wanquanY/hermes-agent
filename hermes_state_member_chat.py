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

import json
import sqlite3
from typing import Any, Dict, List, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


# Sentinel for the team leader's participant view.
LEADER_PARTICIPANT_ID = "leader"


def _coerce_metadata(meta: Any) -> Dict[str, Any]:
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            parsed = json.loads(meta)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _is_viewer_own_assistant(message: Dict[str, Any], viewer: str) -> bool:
    """Decide whether an ``assistant`` row is the viewer's own past turn.

    Heuristic by viewer kind:
      - viewer == LEADER: the leader writes directly into the conv session
        with NO ``team_mission.member_id``, so any assistant row that lacks a
        member_id and whose ``kind`` is neither ``member_chat`` nor
        ``leader_mirror`` is leader-authored.
      - viewer == <member_id>: any row whose
        ``metadata.team_mission.member_id`` matches.
    """
    meta = _coerce_metadata(message.get("metadata"))
    team_meta = meta.get("team_mission") if isinstance(meta.get("team_mission"), dict) else {}
    src_member_id = _text(team_meta.get("member_id"))
    kind = _text(team_meta.get("kind"))
    if not viewer:
        return False
    if viewer == LEADER_PARTICIPANT_ID:
        return not src_member_id and kind not in {"member_chat", "leader_mirror"}
    return bool(src_member_id) and src_member_id == viewer


def _should_skip_for_viewer(message: Dict[str, Any], viewer: str) -> bool:
    """Skip rules independent of role — rows the viewer should not see at
    all (own reply that already exists locally, in-flight @-request the
    worker will receive via run.submit text)."""
    meta = _coerce_metadata(message.get("metadata"))
    # Never re-project a row WE wrote as a view (avoid speaker-prefix
    # nesting on resync).
    if meta.get("member_chat_view"):
        return True
    team_meta = meta.get("team_mission") if isinstance(meta.get("team_mission"), dict) else {}
    kind = _text(team_meta.get("kind"))
    src_member_id = _text(team_meta.get("member_id"))
    target_member_id = _text(team_meta.get("target_member_id"))
    viewer = _text(viewer)
    if kind == "member_chat" and src_member_id and src_member_id == viewer:
        return True
    if kind == "member_chat_user" and target_member_id and target_member_id == viewer:
        return True
    return False


def project_messages_for_viewer(
    messages: List[Dict[str, Any]],
    viewer_participant_id: str,
) -> List[Dict[str, Any]]:
    """Project a conversation's shared message log into the first-person
    history a single participant should hydrate from.

    Stateful walk — preserves the structural integrity of tool-call
    sequences (``assistant`` with tool_calls + paired ``tool`` rows must
    stay together or the model errors on unmatched tool_call_id):

      - ``role=user`` rows: kept (subject to the per-viewer skip rules
        above; the in-flight @-request to this viewer is dropped because
        the worker re-appends it via ``run.submit text=``).
      - ``role=assistant`` rows authored by the viewer: kept VERBATIM —
        content, tool_calls, reasoning, the lot. Subsequent ``tool`` rows
        belong to this turn and are kept.
      - ``role=assistant`` rows authored by anybody else: rewritten as
        ``role=user`` with a ``[<speaker> 在群聊里说] <content>`` prefix
        so the LLM sees observed group-chat speech instead of impersonating
        them. Any ``tool`` rows that immediately follow that other-author
        assistant are theirs and get dropped (the viewer never invoked
        those tools; surfacing the responses would confuse the model and
        waste tokens).
      - ``role=tool`` rows: kept iff the most-recent ``assistant`` we
        emitted was the viewer's own — otherwise discarded.
      - Other roles (``system`` etc.): kept verbatim.

    Empty-content rows are kept for the viewer's own assistant (a
    tool-only turn legitimately has empty content). Other-author rows with
    empty content are dropped (nothing to reflect).
    """
    out: List[Dict[str, Any]] = []
    drop_following_tools = False  # True after we projected an "other" assistant
    viewer = _text(viewer_participant_id)

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = _text(message.get("role")).lower()

        if role == "tool":
            if drop_following_tools:
                continue
            out.append(dict(message))
            continue

        # A non-tool row delimits the previous turn's tail.
        drop_following_tools = False

        if _should_skip_for_viewer(message, viewer):
            continue

        if role == "user":
            content = _text(message.get("content"))
            if not content:
                continue
            new_msg = dict(message)
            new_msg["role"] = "user"
            new_msg["content"] = content
            out.append(new_msg)
            continue

        if role == "assistant":
            if _is_viewer_own_assistant(message, viewer):
                # Verbatim — keeps tool_calls / reasoning intact so the
                # paired ``tool`` rows that follow stay valid.
                out.append(dict(message))
                continue
            # Other speaker — rewrite as observed user-side speech and
            # mark the following tool rows for skip.
            content = _text(message.get("content"))
            if not content:
                # No surfaceable text and not ours — drop the row entirely
                # and also drop any tool rows it would have brought.
                drop_following_tools = True
                continue
            meta = _coerce_metadata(message.get("metadata"))
            team_meta = meta.get("team_mission") if isinstance(meta.get("team_mission"), dict) else {}
            speaker = _text(team_meta.get("display_name"))
            if not speaker:
                speaker = _text(team_meta.get("member_id")) or "Leader"
            new_msg = dict(message)
            new_msg["role"] = "user"
            new_msg["content"] = f"[{speaker} 在群聊里说] {content}"
            # Drop tool_calls/reasoning fields so the row stops looking
            # like a model turn — it's user-side speech now.
            new_msg.pop("tool_calls", None)
            new_msg.pop("tool_call_id", None)
            new_msg.pop("reasoning", None)
            new_msg.pop("reasoning_content", None)
            new_msg.pop("reasoning_details", None)
            out.append(new_msg)
            drop_following_tools = True
            continue

        # Anything else (system, etc.) — passthrough.
        out.append(dict(message))

    return out


def project_message_for_viewer(
    message: Dict[str, Any],
    viewer_participant_id: str,
) -> Optional[Dict[str, Any]]:
    """Single-message convenience for callers that don't care about
    tool-pairing (e.g. sync_member_chat_conversation_view, which works on
    write-time rows that never carry tool_calls). Delegates to the stateful
    walker via a one-element list."""
    projected = project_messages_for_viewer([message], viewer_participant_id)
    return projected[0] if projected else None


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
            meta = _coerce_metadata(m.get("metadata"))
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
