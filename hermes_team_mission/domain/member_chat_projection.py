"""Participant-view projection for member chat hydration."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


LEADER_PARTICIPANT_ID = "leader"


def coerce_message_metadata(meta: Any) -> Dict[str, Any]:
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
    meta = coerce_message_metadata(message.get("metadata"))
    team_meta = meta.get("team_mission") if isinstance(meta.get("team_mission"), dict) else {}
    src_member_id = _text(team_meta.get("member_id"))
    kind = _text(team_meta.get("kind"))
    if not viewer:
        return False
    if viewer == LEADER_PARTICIPANT_ID:
        return not src_member_id and kind not in {"member_chat", "leader_mirror"}
    return bool(src_member_id) and src_member_id == viewer


def _should_skip_for_viewer(message: Dict[str, Any], viewer: str) -> bool:
    meta = coerce_message_metadata(message.get("metadata"))
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
    """Project a shared conversation log into one participant's first-person history."""

    out: List[Dict[str, Any]] = []
    drop_following_tools = False
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
                out.append(dict(message))
                continue
            content = _text(message.get("content"))
            if not content:
                drop_following_tools = True
                continue
            meta = coerce_message_metadata(message.get("metadata"))
            team_meta = meta.get("team_mission") if isinstance(meta.get("team_mission"), dict) else {}
            speaker = _text(team_meta.get("display_name"))
            if not speaker:
                speaker = _text(team_meta.get("member_id")) or "Leader"
            new_msg = dict(message)
            new_msg["role"] = "user"
            new_msg["content"] = f"[{speaker} 在群聊里说] {content}"
            new_msg.pop("tool_calls", None)
            new_msg.pop("tool_call_id", None)
            new_msg.pop("reasoning", None)
            new_msg.pop("reasoning_content", None)
            new_msg.pop("reasoning_details", None)
            out.append(new_msg)
            drop_following_tools = True
            continue

        out.append(dict(message))

    return out


def project_message_for_viewer(
    message: Dict[str, Any],
    viewer_participant_id: str,
) -> Optional[Dict[str, Any]]:
    projected = project_messages_for_viewer([message], viewer_participant_id)
    return projected[0] if projected else None


__all__ = [
    "LEADER_PARTICIPANT_ID",
    "coerce_message_metadata",
    "project_message_for_viewer",
    "project_messages_for_viewer",
]
