"""Channel session identity contracts.

Channel adapters use these types to describe the origin of an inbound message
and derive the deterministic gateway session key for that origin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from channels.config import HomeChannel, Platform
from channels.whatsapp_identity import canonical_whatsapp_identifier


@dataclass
class SessionSource:
    """Describes where a message originated from."""

    platform: Platform
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None
    chat_topic: Optional[str] = None
    user_id_alt: Optional[str] = None
    chat_id_alt: Optional[str] = None
    is_bot: bool = False
    # Platform-neutral server/workspace discriminator. ``guild_id`` remains a
    # temporary wire alias while older adapters and persisted events migrate.
    scope_id: Optional[str] = None
    guild_id: Optional[str] = None
    parent_chat_id: Optional[str] = None
    message_id: Optional[str] = None
    role_authorized: bool = False
    # Named gateway profile that owns this source.  This namespaces sessions
    # and later selects the isolated runtime/config/credential scope.
    profile: Optional[str] = None
    auto_thread_created: bool = False
    auto_thread_initial_name: Optional[str] = None

    def __post_init__(self) -> None:
        """Keep the canonical scope field and its legacy alias coherent."""
        if self.scope_id is None and self.guild_id is not None:
            self.scope_id = self.guild_id
        elif self.scope_id is not None:
            self.guild_id = self.scope_id

    @property
    def description(self) -> str:
        """Human-readable description of the source."""
        if self.platform == Platform.LOCAL:
            return "CLI terminal"

        parts = []
        if self.chat_type == "dm":
            parts.append(f"DM with {self.user_name or self.user_id or 'user'}")
        elif self.chat_type == "group":
            parts.append(f"group: {self.chat_name or self.chat_id}")
        elif self.chat_type == "channel":
            parts.append(f"channel: {self.chat_name or self.chat_id}")
        else:
            parts.append(self.chat_name or self.chat_id)

        if self.thread_id:
            parts.append(f"thread: {self.thread_id}")

        return ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "thread_id": self.thread_id,
            "chat_topic": self.chat_topic,
        }
        if self.user_id_alt:
            d["user_id_alt"] = self.user_id_alt
        if self.chat_id_alt:
            d["chat_id_alt"] = self.chat_id_alt
        scope = self.scope_id if self.scope_id is not None else self.guild_id
        if scope:
            d["scope_id"] = scope
            d["guild_id"] = scope
        if self.parent_chat_id:
            d["parent_chat_id"] = self.parent_chat_id
        if self.message_id:
            d["message_id"] = self.message_id
        if self.role_authorized:
            d["role_authorized"] = True
        if self.profile:
            d["profile"] = self.profile
        if self.auto_thread_created:
            d["auto_thread_created"] = True
        if self.auto_thread_initial_name:
            d["auto_thread_initial_name"] = self.auto_thread_initial_name
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionSource":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            chat_name=data.get("chat_name"),
            chat_type=data.get("chat_type", "dm"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
            thread_id=data.get("thread_id"),
            chat_topic=data.get("chat_topic"),
            user_id_alt=data.get("user_id_alt"),
            chat_id_alt=data.get("chat_id_alt"),
            scope_id=data.get("scope_id", data.get("guild_id")),
            parent_chat_id=data.get("parent_chat_id"),
            message_id=data.get("message_id"),
            role_authorized=bool(data.get("role_authorized", False)),
            profile=data.get("profile"),
            auto_thread_created=bool(data.get("auto_thread_created", False)),
            auto_thread_initial_name=data.get("auto_thread_initial_name"),
        )


@dataclass
class SessionContext:
    """Full context for a session, used for dynamic system prompt injection."""

    source: SessionSource
    connected_platforms: List[Platform]
    home_channels: Dict[Platform, HomeChannel]
    shared_multi_user_session: bool = False
    session_key: str = ""
    session_id: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "connected_platforms": [p.value for p in self.connected_platforms],
            "home_channels": {
                p.value: hc.to_dict() for p, hc in self.home_channels.items()
            },
            "shared_multi_user_session": self.shared_multi_user_session,
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def is_shared_multi_user_session(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> bool:
    """Return True when a non-DM session is shared across participants."""
    if source.chat_type == "dm":
        return False
    if source.thread_id:
        return not thread_sessions_per_user
    return not group_sessions_per_user


def build_session_key(
    source: SessionSource,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
    profile: Optional[str] = None,
) -> str:
    """Build a deterministic session key from a message source."""
    effective_profile = (
        profile if profile is not None else getattr(source, "profile", None)
    )
    namespace = (
        "agent:main"
        if not effective_profile or effective_profile == "default"
        else f"agent:{effective_profile}"
    )
    platform = source.platform.value
    if source.chat_type == "dm":
        dm_chat_id = source.chat_id
        if source.platform == Platform.WHATSAPP:
            dm_chat_id = canonical_whatsapp_identifier(source.chat_id)

        if dm_chat_id:
            if source.thread_id:
                return f"{namespace}:{platform}:dm:{dm_chat_id}:{source.thread_id}"
            return f"{namespace}:{platform}:dm:{dm_chat_id}"
        dm_participant_id = getattr(source, "user_id_alt", None) or source.user_id
        if dm_participant_id and source.platform == Platform.WHATSAPP:
            dm_participant_id = (
                canonical_whatsapp_identifier(str(dm_participant_id))
                or dm_participant_id
            )
        if dm_participant_id:
            if source.thread_id:
                return (
                    f"{namespace}:{platform}:dm:"
                    f"{dm_participant_id}:{source.thread_id}"
                )
            return f"{namespace}:{platform}:dm:{dm_participant_id}"
        if source.thread_id:
            return f"{namespace}:{platform}:dm:{source.thread_id}"
        return f"{namespace}:{platform}:dm"

    participant_id = getattr(source, "user_id_alt", None) or source.user_id
    if participant_id and source.platform == Platform.WHATSAPP:
        participant_id = canonical_whatsapp_identifier(str(participant_id)) or participant_id
    key_parts = [namespace, platform, source.chat_type]

    if source.chat_id:
        key_parts.append(source.chat_id)
    if source.thread_id:
        key_parts.append(source.thread_id)

    isolate_user = group_sessions_per_user
    if source.thread_id and not thread_sessions_per_user:
        isolate_user = False

    if isolate_user and participant_id:
        key_parts.append(str(participant_id))

    return ":".join(key_parts)
