"""Pure Team Mission rules for the user-visible conversation transcript."""

from __future__ import annotations

from typing import Any


MAIN_TRANSCRIPT_ACTIVITY_KINDS = frozenset({
    "leader_chat",
    "member_direct_chat",
    "team_dispatch",
    "mission_start",
    "mission_summary",
    "mission_report",
})
MISSION_NODE_TRANSCRIPT_ACTIVITY_KIND = "mission_node"


class TeamMissionTranscriptVisibilityPolicy:
    """Selects the shared conversation transcript from Team Mission messages."""

    def includes(self, message: dict[str, Any]) -> bool:
        return bool(main_transcript_message_decision(message)["include"])

    def stable_message_id(self, message: dict[str, Any]) -> str:
        metadata = _mapping(message.get("metadata"))
        return _text(
            message.get("conversation_message_id")
            or message.get("conversationMessageId")
            or metadata.get("conversation_message_id")
            or metadata.get("conversationMessageId")
        )


def main_transcript_activity_kind(message: dict[str, Any]) -> str:
    metadata = _mapping(message.get("metadata"))
    return _text(
        metadata.get("transcript_activity_kind")
        or metadata.get("transcriptActivityKind")
        or metadata.get("activity_kind")
        or metadata.get("activityKind")
    )


def main_transcript_message_decision(message: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(message.get("metadata"))
    kind = main_transcript_activity_kind(message)
    activity_kind = _text(metadata.get("activity_kind") or metadata.get("activityKind"))
    if kind:
        include = kind in MAIN_TRANSCRIPT_ACTIVITY_KINDS
        return {
            "include": include,
            "reason": "activity_kind_allowlist"
            if include
            else "activity_kind_excluded",
            "transcript_activity_kind": kind,
            "activity_kind": activity_kind,
        }

    activity_id = _text(metadata.get("activity_id") or metadata.get("activityId"))
    if activity_id.startswith("act-node:"):
        return {
            "include": False,
            "reason": "activity_id_node",
            "transcript_activity_kind": MISSION_NODE_TRANSCRIPT_ACTIVITY_KIND,
            "activity_kind": activity_kind,
        }

    return {
        "include": True,
        "reason": "legacy_no_activity_kind",
        "transcript_activity_kind": "",
        "activity_kind": activity_kind,
    }


def is_main_transcript_message(message: dict[str, Any]) -> bool:
    return bool(main_transcript_message_decision(message)["include"])


def is_node_transcript_message(message: dict[str, Any]) -> bool:
    return not is_main_transcript_message(message)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "MAIN_TRANSCRIPT_ACTIVITY_KINDS",
    "MISSION_NODE_TRANSCRIPT_ACTIVITY_KIND",
    "TeamMissionTranscriptVisibilityPolicy",
    "is_main_transcript_message",
    "is_node_transcript_message",
    "main_transcript_activity_kind",
    "main_transcript_message_decision",
]
