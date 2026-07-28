"""Deterministic profile routing for multiplexed gateway sources."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileRoute:
    """Map a platform scope to one isolated Hermes profile."""

    name: str
    platform: str
    profile: str
    scope_id: Optional[str] = None
    guild_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    enabled: bool = True

    def __post_init__(self) -> None:
        """Normalize the Discord-era alias into the canonical scope field."""
        canonical = self.scope_id if self.scope_id is not None else self.guild_id
        object.__setattr__(self, "scope_id", canonical)
        object.__setattr__(self, "guild_id", canonical)

    @property
    def specificity(self) -> int:
        score = 0
        if self.scope_id:
            score += 2
        if self.chat_id:
            score += 4
        if self.thread_id:
            score += 8
        return score

    def matches(
        self,
        platform: str,
        *,
        scope_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        parent_chat_id: Optional[str] = None,
    ) -> bool:
        """Require every configured discriminator to match conjunctively."""
        actual_scope = scope_id if scope_id is not None else guild_id
        if not self.enabled or self.platform != platform:
            return False
        if self.thread_id and self.thread_id != thread_id:
            return False
        if (
            self.chat_id
            and self.chat_id != chat_id
            and self.chat_id != parent_chat_id
        ):
            return False
        if self.scope_id and self.scope_id != actual_scope:
            return False
        return True


def parse_profile_routes(
    raw: Optional[List[Dict[str, Any]]],
) -> List[ProfileRoute]:
    """Validate routes and return them most-specific-first."""
    if not raw:
        return []
    routes: List[ProfileRoute] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        platform = str(entry.get("platform") or "").strip().lower()
        profile = str(entry.get("profile") or "").strip()
        if not platform or not profile:
            logger.warning(
                "Skipping profile route %s: missing platform or profile",
                name,
            )
            continue
        try:
            from hermes_cli.profiles import (
                normalize_profile_name,
                validate_profile_name,
            )

            profile = normalize_profile_name(profile)
            validate_profile_name(profile)
        except (ImportError, ValueError):
            logger.warning(
                "Skipping profile route %s: invalid profile name %r",
                name,
                profile,
            )
            continue
        routes.append(
            ProfileRoute(
                name=name,
                platform=platform,
                profile=profile,
                scope_id=_optional_text(
                    entry.get("scope_id", entry.get("guild_id"))
                ),
                chat_id=_optional_text(entry.get("chat_id")),
                thread_id=_optional_text(entry.get("thread_id")),
                enabled=_coerce_enabled(entry.get("enabled", True)),
            )
        )
    routes.sort(key=lambda route: route.specificity, reverse=True)
    return routes


def match_profile_route(
    routes: List[ProfileRoute],
    platform: str,
    *,
    scope_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    parent_chat_id: Optional[str] = None,
) -> Optional[ProfileRoute]:
    for route in routes:
        if route.matches(
            platform,
            scope_id=scope_id,
            guild_id=guild_id,
            chat_id=chat_id,
            thread_id=thread_id,
            parent_chat_id=parent_chat_id,
        ):
            return route
    return None


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _coerce_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return bool(value)
