"""Conversation participant identity helpers."""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def user_participant_id(user_id: str = "") -> str:
    stable = _text(user_id)
    if not stable or stable == "default":
        return "user"
    if stable.startswith("user:"):
        return stable
    return f"user:{stable}"


def leader_participant_id(conversation_id: str) -> str:
    """Return the leader identity owned by one visible team conversation."""
    stable = _text(conversation_id)
    if not stable:
        raise ValueError("conversation_id required for leader participant")
    if stable.startswith("leader:"):
        return stable
    return f"leader:{stable}"


def member_participant_id(member_id: str) -> str:
    stable = _text(member_id)
    if not stable:
        raise ValueError("member_id required")
    if stable.startswith("member:"):
        return stable
    return f"member:{stable}"


def agent_participant_id(agent_profile_id: str = "") -> str:
    stable = _text(agent_profile_id)
    if not stable or stable == "default":
        return "agent"
    if stable.startswith("agent:"):
        return stable
    return f"agent:{stable}"


__all__ = [
    "agent_participant_id",
    "leader_participant_id",
    "member_participant_id",
    "user_participant_id",
]
