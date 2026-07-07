"""AgentProfileRepo protocol + concrete impl (spec §4.5) — agent profiles."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hermes_agent.repositories.base import RepositoryConnection


@dataclass(frozen=True)
class ProfileSpec:
    """Creation payload for a new agent profile (spec §4.5)."""

    profile_id: str
    slug: str
    name: str
    hermes_home_path: str
    status: str = "active"
    category: str = ""
    tags: tuple[str, ...] = ()
    description: str = ""
    avatar: str = ""
    is_system_default: bool = False
    hermes_profile_name: str = ""
    default_model: str = ""


@dataclass(frozen=True)
class ProfileVersion:
    """A version snapshot of a profile."""

    profile_id: str
    version_id: str
    version_number: int
    payload_json: str = ""
    created_at: float = 0.0
    is_current: bool = False


@dataclass(frozen=True)
class Profile:
    profile_id: str
    slug: str
    name: str
    status: str
    hermes_home_path: str
    category: str = ""
    tags: tuple[str, ...] = ()
    description: str = ""
    avatar: str = ""
    is_system_default: bool = False
    default_model: str = ""
    current_version_id: str = ""
    current_version_number: int = 0


@dataclass(frozen=True)
class GrowthSummary:
    profile_id: str
    total_runs: int = 0
    total_messages: int = 0
    total_tokens: int = 0
    growth_score: float = 0.0
    updated_at: float = 0.0
    fields: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentProfileRepo(Protocol):
    def create_profile(self, spec: ProfileSpec) -> Profile: ...

    def get(self, profile_id: str) -> Profile | None: ...

    def add_version(self, profile_id: str, version: ProfileVersion) -> None: ...

    def get_growth_summary(self, profile_id: str) -> GrowthSummary: ...


class AgentProfileRepoImpl:
    """SQLite-backed AgentProfileRepo (spec §4.5)."""

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def create_profile(self, spec: ProfileSpec) -> Profile:
        stable = str(spec.profile_id or "").strip()
        if not stable:
            raise ValueError("ProfileSpec.profile_id is required")
        if not str(spec.slug or "").strip():
            raise ValueError("ProfileSpec.slug is required")
        if not str(spec.name or "").strip():
            raise ValueError("ProfileSpec.name is required")
        tags_json = json.dumps(list(spec.tags), ensure_ascii=False)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO agent_profiles (
                id, slug, name, avatar, description, category, tags_json,
                status, is_system_default, hermes_profile_name,
                hermes_home_path, default_model,
                current_version_id, current_version_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0)
            """,
            (
                stable,
                str(spec.slug),
                str(spec.name),
                str(spec.avatar or ""),
                str(spec.description or ""),
                str(spec.category or ""),
                tags_json,
                str(spec.status or "active"),
                1 if spec.is_system_default else 0,
                str(spec.hermes_profile_name or ""),
                str(spec.hermes_home_path or ""),
                str(spec.default_model or ""),
            ),
        )
        got = self.get(stable)
        assert got is not None
        return got

    def get(self, profile_id: str) -> Profile | None:
        stable = str(profile_id or "").strip()
        if not stable:
            return None
        row = self._conn.execute(
            """
            SELECT id, slug, name, status, hermes_home_path, category,
                   tags_json, description, avatar, is_system_default,
                   default_model, current_version_id, current_version_number
              FROM agent_profiles
             WHERE id = ?
            """,
            (stable,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_profile(row)

    def add_version(self, profile_id: str, version: ProfileVersion) -> None:
        stable_profile = str(profile_id or "").strip()
        stable_version = str(version.version_id or "").strip()
        if not stable_profile or not stable_version:
            raise ValueError("profile_id and version.version_id are required")
        now = version.created_at or time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO agent_profile_versions (
                profile_id, version_id, version_number, payload_json,
                created_at, is_current
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stable_profile,
                stable_version,
                int(version.version_number),
                str(version.payload_json or ""),
                float(now),
                1 if version.is_current else 0,
            ),
        )
        if version.is_current:
            self._conn.execute(
                """
                UPDATE agent_profiles
                   SET current_version_id = ?,
                       current_version_number = ?
                 WHERE id = ?
                """,
                (stable_version, int(version.version_number), stable_profile),
            )
            # Mark other versions as non-current for this profile.
            self._conn.execute(
                """
                UPDATE agent_profile_versions
                   SET is_current = 0
                 WHERE profile_id = ? AND version_id != ?
                """,
                (stable_profile, stable_version),
            )

    def get_growth_summary(self, profile_id: str) -> GrowthSummary:
        stable = str(profile_id or "").strip()
        if not stable:
            raise ValueError("profile_id is required")
        row = self._conn.execute(
            """
            SELECT profile_id, total_runs, total_messages, total_tokens,
                   growth_score, updated_at
              FROM agent_profile_growth_summary
             WHERE profile_id = ?
            """,
            (stable,),
        ).fetchone()
        if row is None:
            return GrowthSummary(profile_id=stable)
        return GrowthSummary(
            profile_id=stable,
            total_runs=int(row["total_runs"] or 0) if isinstance(row, sqlite3.Row) else int(row[1] or 0),
            total_messages=int(row["total_messages"] or 0) if isinstance(row, sqlite3.Row) else int(row[2] or 0),
            total_tokens=int(row["total_tokens"] or 0) if isinstance(row, sqlite3.Row) else int(row[3] or 0),
            growth_score=float(row["growth_score"] or 0) if isinstance(row, sqlite3.Row) else float(row[4] or 0),
            updated_at=float(row["updated_at"] or 0) if isinstance(row, sqlite3.Row) else float(row[5] or 0),
        )


def _row_to_profile(row: Any) -> Profile:
    def _get(name, idx):
        return row[name] if isinstance(row, sqlite3.Row) else row[idx]

    tags_raw = _get("tags_json", 6) or "[]"
    try:
        tags = tuple(json.loads(tags_raw))
    except json.JSONDecodeError:
        tags = ()
    return Profile(
        profile_id=str(_get("id", 0) or ""),
        slug=str(_get("slug", 1) or ""),
        name=str(_get("name", 2) or ""),
        status=str(_get("status", 3) or "active"),
        hermes_home_path=str(_get("hermes_home_path", 4) or ""),
        category=str(_get("category", 5) or ""),
        tags=tags,
        description=str(_get("description", 7) or ""),
        avatar=str(_get("avatar", 8) or ""),
        is_system_default=bool(int(_get("is_system_default", 9) or 0)),
        default_model=str(_get("default_model", 10) or ""),
        current_version_id=str(_get("current_version_id", 11) or ""),
        current_version_number=int(_get("current_version_number", 12) or 0),
    )


__all__ = [
    "AgentProfileRepo",
    "AgentProfileRepoImpl",
    "GrowthSummary",
    "Profile",
    "ProfileSpec",
    "ProfileVersion",
]
