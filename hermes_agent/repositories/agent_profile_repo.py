"""AgentProfileRepo protocol + concrete impl (spec §4.5) — agent profiles."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from agent.dovie_diagnostics import emit_dovie_diagnostic
from hermes_agent_profile_growth import summarize_agent_profile_growth
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

    def list(self, *, status: str = "", include_archived: bool = False, limit: int = 100) -> list[Profile]: ...

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
        if self._has_profile_registry_schema():
            self.upsert_agent_profile(
                profile_id=stable,
                slug=spec.slug,
                name=spec.name,
                hermes_home_path=spec.hermes_home_path,
                status=spec.status,
                category=spec.category,
                tags=list(spec.tags),
                description=spec.description,
                avatar=spec.avatar,
                is_system_default=spec.is_system_default,
                hermes_profile_name=spec.hermes_profile_name,
                default_model=spec.default_model,
            )
            got = self.get(stable)
            assert got is not None
            return got
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

    def list(
        self,
        *,
        status: str = "",
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[Profile]:
        bounded_limit = max(1, min(int(limit or 100), 500))
        clauses: list[str] = []
        args: list[Any] = []
        status_filter = _text(status)
        if status_filter:
            clauses.append("status = ?")
            args.append(status_filter)
        elif not include_archived:
            clauses.append("status != 'archived'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT id, slug, name, status, hermes_home_path, category,
                   tags_json, description, avatar, is_system_default,
                   default_model, current_version_id, current_version_number
              FROM agent_profiles
             {where}
             ORDER BY id ASC
             LIMIT ?
            """,
            (*args, bounded_limit),
        ).fetchall()
        return [_row_to_profile(row) for row in rows]

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

    def upsert_agent_profile(
        self,
        *,
        profile_id: str,
        slug: str,
        name: str,
        avatar: str = "",
        description: str = "",
        category: str = "",
        tags: list[str] | None = None,
        status: str = "active",
        is_system_default: bool = False,
        hermes_profile_name: str = "",
        hermes_home_path: str,
        default_model: str = "",
        default_provider: str = "",
        default_permission_mode: str = "default",
        default_toolsets: list[str] | None = None,
        recommended_skills: list[str] | None = None,
        platform_base_toolsets_initialized: bool = False,
        current_version_id: str = "",
        current_version_number: int = 0,
        source_kind: str = "",
        public_profile_id: str = "",
        public_version_id: str = "",
        public_content_hash: str = "",
        metadata: dict[str, Any] | None = None,
        created_at: float | str | None = None,
        updated_at: float | str | None = None,
        last_used_at: float | str | None = None,
    ) -> dict[str, Any]:
        resolved_profile_id = _text(profile_id)
        resolved_slug = _text(slug)
        resolved_name = _text(name)
        resolved_home = _text(hermes_home_path)
        if not resolved_profile_id:
            raise ValueError("profile_id required")
        if not resolved_slug:
            raise ValueError("profile slug required")
        if not resolved_name:
            raise ValueError("profile name required")
        if not resolved_home:
            raise ValueError("profile hermes_home_path required")
        now = time.time()
        created = _timestamp(created_at or now)
        updated = _timestamp(updated_at or now)
        last_used = _timestamp(last_used_at) if last_used_at else None
        existing = self._conn.execute(
            "SELECT created_at FROM agent_profiles WHERE id = ?",
            (resolved_profile_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO agent_profiles (
                id, slug, name, avatar, description, category, tags_json,
                status, is_system_default, hermes_profile_name,
                hermes_home_path, default_model, default_provider,
                default_permission_mode, default_toolsets_json,
                recommended_skills_json,
                platform_base_toolsets_initialized, current_version_id,
                current_version_number, source_kind, public_profile_id,
                public_version_id, public_content_hash, metadata_json,
                created_at, updated_at, last_used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                slug = excluded.slug,
                name = excluded.name,
                avatar = excluded.avatar,
                description = excluded.description,
                category = excluded.category,
                tags_json = excluded.tags_json,
                status = excluded.status,
                is_system_default = excluded.is_system_default,
                hermes_profile_name = excluded.hermes_profile_name,
                hermes_home_path = excluded.hermes_home_path,
                default_model = excluded.default_model,
                default_provider = excluded.default_provider,
                default_permission_mode = excluded.default_permission_mode,
                default_toolsets_json = excluded.default_toolsets_json,
                recommended_skills_json = excluded.recommended_skills_json,
                platform_base_toolsets_initialized = excluded.platform_base_toolsets_initialized,
                current_version_id = excluded.current_version_id,
                current_version_number = excluded.current_version_number,
                source_kind = excluded.source_kind,
                public_profile_id = excluded.public_profile_id,
                public_version_id = excluded.public_version_id,
                public_content_hash = excluded.public_content_hash,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at,
                last_used_at = excluded.last_used_at
            """,
            (
                resolved_profile_id,
                resolved_slug,
                resolved_name,
                _text(avatar),
                _text(description),
                _text(category),
                _json_dumps(_string_list(tags or [])),
                "archived" if _text(status) == "archived" else "active",
                1 if is_system_default else 0,
                _text(hermes_profile_name),
                resolved_home,
                _text(default_model),
                _text(default_provider),
                _text(default_permission_mode) or "default",
                _json_dumps(_string_list(default_toolsets or [])),
                _json_dumps(_string_list(recommended_skills or [])),
                1 if platform_base_toolsets_initialized else 0,
                _text(current_version_id),
                max(0, _safe_int(current_version_number, 0)),
                _text(source_kind),
                _text(public_profile_id),
                _text(public_version_id),
                _text(public_content_hash),
                _json_dumps(_object(metadata)),
                float(_row_value(existing, "created_at", created) or created),
                updated,
                last_used,
            ),
        )
        self._conn.commit()
        return self.get_agent_profile(resolved_profile_id)

    def get_agent_profile(self, profile_id: str) -> dict[str, Any]:
        normalized = _text(profile_id)
        if not normalized:
            return {}
        row = self._conn.execute(
            "SELECT * FROM agent_profiles WHERE id = ?",
            (normalized,),
        ).fetchone()
        return _agent_profile_from_row(row)

    def get_agent_profile_by_slug(self, slug: str) -> dict[str, Any]:
        normalized = _text(slug)
        if not normalized:
            return {}
        row = self._conn.execute(
            """
            SELECT * FROM agent_profiles
            WHERE slug = ? OR hermes_profile_name = ?
            ORDER BY status = 'active' DESC, updated_at DESC
            LIMIT 1
            """,
            (normalized, normalized),
        ).fetchone()
        return _agent_profile_from_row(row)

    def list_agent_profiles(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        if include_archived:
            rows = self._conn.execute(
                "SELECT * FROM agent_profiles ORDER BY updated_at DESC, id ASC",
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM agent_profiles
                WHERE status != 'archived'
                ORDER BY updated_at DESC, id ASC
                """,
            ).fetchall()
        return [profile for profile in (_agent_profile_from_row(row) for row in rows) if profile]

    def archive_agent_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self.get_agent_profile(profile_id)
        if not profile:
            return {}
        return self.upsert_agent_profile(
            profile_id=profile["id"],
            slug=profile["slug"],
            name=profile["name"],
            avatar=profile.get("avatar", ""),
            description=profile.get("description", ""),
            category=profile.get("category", ""),
            tags=profile.get("tags") if isinstance(profile.get("tags"), list) else [],
            status="archived",
            is_system_default=bool(profile.get("is_system_default") or profile.get("isSystemDefault")),
            hermes_profile_name=profile.get("hermes_profile_name", ""),
            hermes_home_path=profile.get("hermes_home_path", ""),
            default_model=profile.get("default_model", ""),
            default_provider=profile.get("default_provider", ""),
            default_permission_mode=profile.get("default_permission_mode", ""),
            default_toolsets=profile.get("default_toolsets") if isinstance(profile.get("default_toolsets"), list) else [],
            recommended_skills=profile.get("recommended_skills") if isinstance(profile.get("recommended_skills"), list) else [],
            platform_base_toolsets_initialized=bool(profile.get("platform_base_toolsets_initialized")),
            current_version_id=profile.get("current_version_id", ""),
            current_version_number=_safe_int(profile.get("current_version_number", 0), 0),
            source_kind=profile.get("source_kind", ""),
            public_profile_id=profile.get("public_profile_id", ""),
            public_version_id=profile.get("public_version_id", ""),
            public_content_hash=profile.get("public_content_hash", ""),
            metadata=profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {},
            created_at=profile.get("created_at"),
            last_used_at=profile.get("last_used_at"),
        )

    def agent_profile_growth_summary(
        self,
        agent_profile_id: str,
        *,
        agent_profile_version_id: str = "",
        range_preset: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any]:
        profile = self.get_agent_profile(agent_profile_id)
        if not profile:
            emit_dovie_diagnostic(
                "[profile-growth-summary]",
                {
                    "stage": "registry_profile_missing",
                    "profile_id": _text(agent_profile_id),
                    "requested_version_id": _text(agent_profile_version_id),
                },
            )
            return {}
        emit_dovie_diagnostic(
            "[profile-growth-summary]",
            {
                "stage": "registry_lookup_finished",
                "profile_id": _text(profile.get("id")),
                "requested_version_id": _text(agent_profile_version_id),
                "profile_home": _text(profile.get("hermesHomePath") or profile.get("hermes_home_path")),
                "profile_runtime_home": _text(profile.get("runtimeHomePath") or profile.get("runtime_home_path")),
            },
        )
        return summarize_agent_profile_growth(
            profile,
            version={},
            range_options={
                "rangePreset": _text(range_preset),
                "startDate": _text(start_date),
                "endDate": _text(end_date),
            },
        )

    def upsert_agent_profile_draft(
        self,
        *,
        draft_id: str,
        status: str = "draft",
        draft_kind: str = "create",
        base_agent_profile_id: str = "",
        base_version_id: str = "",
        target_agent_profile_id: str = "",
        source_session_id: str = "",
        source_agent_profile_id: str = "",
        source_run_id: str = "",
        source_turn_id: str = "",
        source_client_message_id: str = "",
        workspace_id: str = "",
        name: str,
        avatar: str = "",
        description: str = "",
        category: str = "",
        tags: list[str] | None = None,
        architecture_template_id: str = "",
        recommended_toolsets: list[str] | None = None,
        recommended_skills: list[str] | None = None,
        skill_creation_plans: list[dict[str, Any]] | None = None,
        missing_capabilities: list[str] | None = None,
        default_model: str = "",
        default_provider: str = "",
        default_permission_mode: str = "default",
        files: dict[str, Any] | None = None,
        runtime_prepared_at: float | str | None = None,
        published_agent_profile_id: str = "",
        published_version_id: str = "",
        metadata: dict[str, Any] | None = None,
        created_at: float | str | None = None,
        updated_at: float | str | None = None,
        published_at: float | str | None = None,
    ) -> dict[str, Any]:
        resolved_draft_id = _text(draft_id)
        resolved_name = _text(name)
        if not resolved_draft_id:
            raise ValueError("draft_id required")
        if not resolved_name:
            raise ValueError("draft name required")
        now = time.time()
        created = _timestamp(created_at or now)
        updated = _timestamp(updated_at or now)
        runtime_prepared = _timestamp(runtime_prepared_at) if runtime_prepared_at else None
        published = _timestamp(published_at) if published_at else None
        existing = self._conn.execute(
            "SELECT created_at FROM agent_profile_drafts WHERE id = ?",
            (resolved_draft_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO agent_profile_drafts (
                id, status, draft_kind, base_agent_profile_id,
                base_version_id, target_agent_profile_id, source_session_id,
                source_agent_profile_id, source_run_id, source_turn_id,
                source_client_message_id, workspace_id, name, avatar,
                description, category, tags_json, architecture_template_id,
                recommended_toolsets_json, recommended_skills_json,
                skill_creation_plans_json, missing_capabilities_json,
                default_model, default_provider, default_permission_mode,
                files_json, runtime_prepared_at, published_agent_profile_id,
                published_version_id, metadata_json, created_at, updated_at,
                published_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                draft_kind = excluded.draft_kind,
                base_agent_profile_id = excluded.base_agent_profile_id,
                base_version_id = excluded.base_version_id,
                target_agent_profile_id = excluded.target_agent_profile_id,
                source_session_id = excluded.source_session_id,
                source_agent_profile_id = excluded.source_agent_profile_id,
                source_run_id = excluded.source_run_id,
                source_turn_id = excluded.source_turn_id,
                source_client_message_id = excluded.source_client_message_id,
                workspace_id = excluded.workspace_id,
                name = excluded.name,
                avatar = excluded.avatar,
                description = excluded.description,
                category = excluded.category,
                tags_json = excluded.tags_json,
                architecture_template_id = excluded.architecture_template_id,
                recommended_toolsets_json = excluded.recommended_toolsets_json,
                recommended_skills_json = excluded.recommended_skills_json,
                skill_creation_plans_json = excluded.skill_creation_plans_json,
                missing_capabilities_json = excluded.missing_capabilities_json,
                default_model = excluded.default_model,
                default_provider = excluded.default_provider,
                default_permission_mode = excluded.default_permission_mode,
                files_json = excluded.files_json,
                runtime_prepared_at = excluded.runtime_prepared_at,
                published_agent_profile_id = excluded.published_agent_profile_id,
                published_version_id = excluded.published_version_id,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at,
                published_at = excluded.published_at
            """,
            (
                resolved_draft_id,
                _draft_status(status),
                _draft_kind(draft_kind, has_revision_target=bool(base_agent_profile_id or base_version_id or target_agent_profile_id)),
                _text(base_agent_profile_id),
                _text(base_version_id),
                _text(target_agent_profile_id),
                _text(source_session_id),
                _text(source_agent_profile_id),
                _text(source_run_id),
                _text(source_turn_id),
                _text(source_client_message_id),
                _text(workspace_id),
                resolved_name,
                _text(avatar),
                _text(description),
                _text(category),
                _json_dumps(_string_list(tags or [])),
                _text(architecture_template_id),
                _json_dumps(_string_list(recommended_toolsets or [])),
                _json_dumps(_string_list(recommended_skills or [])),
                _json_dumps([_object(item) for item in (skill_creation_plans or [])]),
                _json_dumps(_string_list(missing_capabilities or [])),
                _text(default_model),
                _text(default_provider),
                _text(default_permission_mode) or "default",
                _json_dumps(_object(files)),
                runtime_prepared,
                _text(published_agent_profile_id),
                _text(published_version_id),
                _json_dumps(_object(metadata)),
                float(_row_value(existing, "created_at", created) or created),
                updated,
                published,
            ),
        )
        self._conn.commit()
        return self.get_agent_profile_draft(resolved_draft_id)

    def get_agent_profile_draft(self, draft_id: str) -> dict[str, Any]:
        normalized = _text(draft_id)
        if not normalized:
            return {}
        row = self._conn.execute(
            "SELECT * FROM agent_profile_drafts WHERE id = ?",
            (normalized,),
        ).fetchone()
        return _agent_profile_draft_from_row(row)

    def list_agent_profile_drafts(
        self,
        *,
        include_published: bool = False,
        include_discarded: bool = False,
        statuses: list[str] | None = None,
        source_session_id: str = "",
        source_agent_profile_id: str = "",
        workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[Any] = []
        normalized_statuses = [_draft_status(status) for status in (statuses or [])]
        if normalized_statuses:
            placeholders = ",".join("?" for _ in normalized_statuses)
            filters.append(f"status IN ({placeholders})")
            values.extend(normalized_statuses)
        else:
            excluded = []
            if not include_published:
                excluded.append("published")
            if not include_discarded:
                excluded.append("discarded")
            if excluded:
                placeholders = ",".join("?" for _ in excluded)
                filters.append(f"status NOT IN ({placeholders})")
                values.extend(excluded)
        if _text(source_session_id):
            filters.append("source_session_id = ?")
            values.append(_text(source_session_id))
        if _text(source_agent_profile_id):
            filters.append("source_agent_profile_id = ?")
            values.append(_text(source_agent_profile_id))
        if _text(workspace_id):
            filters.append("workspace_id = ?")
            values.append(_text(workspace_id))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = self._conn.execute(
            f"""
            SELECT * FROM agent_profile_drafts
            {where}
            ORDER BY updated_at DESC, id ASC
            """,
            values,
        ).fetchall()
        return [draft for draft in (_agent_profile_draft_from_row(row) for row in rows) if draft]

    def discard_agent_profile_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.get_agent_profile_draft(draft_id)
        if not draft:
            return {}
        return self.upsert_agent_profile_draft(
            draft_id=draft["id"],
            status="discarded",
            draft_kind=draft.get("draft_kind", "create"),
            base_agent_profile_id=draft.get("base_agent_profile_id", ""),
            base_version_id=draft.get("base_version_id", ""),
            target_agent_profile_id=draft.get("target_agent_profile_id", ""),
            source_session_id=draft.get("source_session_id", ""),
            source_agent_profile_id=draft.get("source_agent_profile_id", ""),
            source_run_id=draft.get("source_run_id", ""),
            source_turn_id=draft.get("source_turn_id", ""),
            source_client_message_id=draft.get("source_client_message_id", ""),
            workspace_id=draft.get("workspace_id", ""),
            name=draft.get("name", ""),
            avatar=draft.get("avatar", ""),
            description=draft.get("description", ""),
            category=draft.get("category", ""),
            tags=draft.get("tags") if isinstance(draft.get("tags"), list) else [],
            architecture_template_id=draft.get("architecture_template_id", ""),
            recommended_toolsets=draft.get("recommended_toolsets") if isinstance(draft.get("recommended_toolsets"), list) else [],
            recommended_skills=draft.get("recommended_skills") if isinstance(draft.get("recommended_skills"), list) else [],
            skill_creation_plans=draft.get("skill_creation_plans") if isinstance(draft.get("skill_creation_plans"), list) else [],
            missing_capabilities=draft.get("missing_capabilities") if isinstance(draft.get("missing_capabilities"), list) else [],
            default_model=draft.get("default_model", ""),
            default_provider=draft.get("default_provider", ""),
            default_permission_mode=draft.get("default_permission_mode", ""),
            files=draft.get("files") if isinstance(draft.get("files"), dict) else {},
            runtime_prepared_at=draft.get("runtime_prepared_at"),
            published_agent_profile_id=draft.get("published_agent_profile_id", ""),
            published_version_id=draft.get("published_version_id", ""),
            metadata=draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {},
            created_at=draft.get("created_at"),
            published_at=draft.get("published_at"),
        )

    def _has_profile_registry_schema(self) -> bool:
        columns = _table_columns(self._conn, "agent_profiles")
        return "metadata_json" in columns and "default_toolsets_json" in columns


def ensure_agent_profile_repository_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the AgentProfileRepo-owned table family."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_profiles (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            avatar TEXT,
            description TEXT,
            category TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            is_system_default INTEGER NOT NULL DEFAULT 0,
            hermes_profile_name TEXT,
            hermes_home_path TEXT NOT NULL DEFAULT '',
            default_model TEXT,
            default_provider TEXT,
            default_permission_mode TEXT,
            default_toolsets_json TEXT NOT NULL DEFAULT '[]',
            recommended_skills_json TEXT NOT NULL DEFAULT '[]',
            platform_base_toolsets_initialized INTEGER NOT NULL DEFAULT 0,
            current_version_id TEXT,
            current_version_number INTEGER NOT NULL DEFAULT 0,
            source_kind TEXT,
            public_profile_id TEXT,
            public_version_id TEXT,
            public_content_hash TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            last_used_at REAL
        );

        CREATE TABLE IF NOT EXISTS agent_profile_versions (
            profile_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            payload_json TEXT,
            created_at REAL NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (profile_id, version_id)
        );

        CREATE TABLE IF NOT EXISTS agent_profile_growth_summary (
            profile_id TEXT PRIMARY KEY,
            total_runs INTEGER NOT NULL DEFAULT 0,
            total_messages INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            growth_score REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS agent_profile_drafts (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            draft_kind TEXT NOT NULL,
            base_agent_profile_id TEXT,
            base_version_id TEXT,
            target_agent_profile_id TEXT,
            source_session_id TEXT,
            source_agent_profile_id TEXT,
            source_run_id TEXT,
            source_turn_id TEXT,
            source_client_message_id TEXT,
            workspace_id TEXT,
            name TEXT NOT NULL,
            avatar TEXT,
            description TEXT,
            category TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            architecture_template_id TEXT,
            recommended_toolsets_json TEXT NOT NULL DEFAULT '[]',
            recommended_skills_json TEXT NOT NULL DEFAULT '[]',
            skill_creation_plans_json TEXT NOT NULL DEFAULT '[]',
            missing_capabilities_json TEXT NOT NULL DEFAULT '[]',
            default_model TEXT,
            default_provider TEXT,
            default_permission_mode TEXT,
            files_json TEXT NOT NULL DEFAULT '{}',
            runtime_prepared_at REAL,
            published_agent_profile_id TEXT,
            published_version_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            published_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_profiles_status_updated
            ON agent_profiles(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_profile_drafts_status_updated
            ON agent_profile_drafts(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_profile_drafts_source
            ON agent_profile_drafts(source_session_id, source_agent_profile_id, updated_at DESC);
        """
    )
    _ensure_columns(
        conn,
        "agent_profiles",
        {
            "default_provider": "TEXT",
            "default_permission_mode": "TEXT",
            "default_toolsets_json": "TEXT NOT NULL DEFAULT '[]'",
            "recommended_skills_json": "TEXT NOT NULL DEFAULT '[]'",
            "platform_base_toolsets_initialized": "INTEGER NOT NULL DEFAULT 0",
            "source_kind": "TEXT",
            "public_profile_id": "TEXT",
            "public_version_id": "TEXT",
            "public_content_hash": "TEXT",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_at": "REAL NOT NULL DEFAULT 0",
            "updated_at": "REAL NOT NULL DEFAULT 0",
            "last_used_at": "REAL",
        },
    )
    _ensure_columns(
        conn,
        "agent_profile_drafts",
        {
            "workspace_id": "TEXT",
            "published_agent_profile_id": "TEXT",
            "published_version_id": "TEXT",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "published_at": "REAL",
        },
    )


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = _table_columns(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _timestamp(value: Any = None) -> float:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0 else time.time()
    text = _text(value)
    if text:
        parsed_float: float | None = None
        try:
            parsed_float = float(text)
        except ValueError:
            parsed_float = None
        if parsed_float is not None:
            return parsed_float if parsed_float > 0 else time.time()
        parsed_dt: datetime | None = None
        try:
            parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed_dt = None
        if parsed_dt is not None:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            return parsed_dt.timestamp()
    return time.time()


def _iso_timestamp(value: Any = None) -> str:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _text(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _draft_status(value: Any) -> str:
    normalized = _text(value)
    if normalized in {"draft", "published", "discarded"}:
        return normalized
    return "draft"


def _draft_kind(value: Any, *, has_revision_target: bool = False) -> str:
    normalized = _text(value)
    if normalized in {"create", "revision", "local_runtime_recovered", "market_package"}:
        return normalized
    return "revision" if has_revision_target else "create"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}


def _agent_profile_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    profile_id = _text(_row_value(row, "id", ""))
    current_version_id = _text(_row_value(row, "current_version_id", ""))
    hermes_home_path = _text(_row_value(row, "hermes_home_path", ""))
    runtime_scope_key = f"profile:{profile_id}" if profile_id else ""
    created_at = float(_row_value(row, "created_at", 0) or 0)
    updated_at = float(_row_value(row, "updated_at", 0) or 0)
    last_used_at = float(_row_value(row, "last_used_at", 0) or 0)
    payload: dict[str, Any] = {
        "id": profile_id,
        "slug": _text(_row_value(row, "slug", "")),
        "name": _text(_row_value(row, "name", "")),
        "avatar": _text(_row_value(row, "avatar", "")),
        "description": _text(_row_value(row, "description", "")),
        "category": _text(_row_value(row, "category", "")),
        "tags": _json_loads(_row_value(row, "tags_json", ""), []),
        "status": _text(_row_value(row, "status", "")) or "active",
        "is_system_default": bool(int(_row_value(row, "is_system_default", 0) or 0)),
        "isSystemDefault": bool(int(_row_value(row, "is_system_default", 0) or 0)),
        "hermes_profile_name": _text(_row_value(row, "hermes_profile_name", "")),
        "hermesProfileName": _text(_row_value(row, "hermes_profile_name", "")),
        "hermes_home_path": hermes_home_path,
        "hermesHomePath": hermes_home_path,
        "default_model": _text(_row_value(row, "default_model", "")),
        "defaultModel": _text(_row_value(row, "default_model", "")),
        "default_provider": _text(_row_value(row, "default_provider", "")),
        "defaultProvider": _text(_row_value(row, "default_provider", "")),
        "default_permission_mode": _text(_row_value(row, "default_permission_mode", "")) or "default",
        "defaultPermissionMode": _text(_row_value(row, "default_permission_mode", "")) or "default",
        "default_toolsets": _json_loads(_row_value(row, "default_toolsets_json", ""), []),
        "defaultToolsets": _json_loads(_row_value(row, "default_toolsets_json", ""), []),
        "recommended_skills": _json_loads(_row_value(row, "recommended_skills_json", ""), []),
        "recommendedSkills": _json_loads(_row_value(row, "recommended_skills_json", ""), []),
        "platform_base_toolsets_initialized": bool(int(_row_value(row, "platform_base_toolsets_initialized", 0) or 0)),
        "platformBaseToolsetsInitialized": bool(int(_row_value(row, "platform_base_toolsets_initialized", 0) or 0)),
        "current_version_id": current_version_id,
        "currentVersionId": current_version_id,
        "agent_profile_version_id": current_version_id,
        "agentProfileVersionId": current_version_id,
        "current_version_number": _safe_int(_row_value(row, "current_version_number", 0)),
        "currentVersionNumber": _safe_int(_row_value(row, "current_version_number", 0)),
        "runtime_home_path": hermes_home_path,
        "runtimeHomePath": hermes_home_path,
        "runtime_scope_key": runtime_scope_key,
        "runtimeScopeKey": runtime_scope_key,
        "source_kind": _text(_row_value(row, "source_kind", "")),
        "sourceKind": _text(_row_value(row, "source_kind", "")),
        "public_profile_id": _text(_row_value(row, "public_profile_id", "")),
        "publicProfileId": _text(_row_value(row, "public_profile_id", "")),
        "public_version_id": _text(_row_value(row, "public_version_id", "")),
        "publicVersionId": _text(_row_value(row, "public_version_id", "")),
        "public_content_hash": _text(_row_value(row, "public_content_hash", "")),
        "publicContentHash": _text(_row_value(row, "public_content_hash", "")),
        "metadata": _json_loads(_row_value(row, "metadata_json", ""), {}),
        "created_at": created_at,
        "createdAt": _iso_timestamp(created_at),
        "updated_at": updated_at,
        "updatedAt": _iso_timestamp(updated_at),
    }
    if last_used_at > 0:
        payload.update({
            "last_used_at": last_used_at,
            "lastUsedAt": _iso_timestamp(last_used_at),
        })
    return payload


def _agent_profile_draft_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    created_at = float(_row_value(row, "created_at", 0) or 0)
    updated_at = float(_row_value(row, "updated_at", 0) or 0)
    runtime_prepared_at = float(_row_value(row, "runtime_prepared_at", 0) or 0)
    published_at = float(_row_value(row, "published_at", 0) or 0)
    payload: dict[str, Any] = {
        "id": _text(_row_value(row, "id", "")),
        "status": _text(_row_value(row, "status", "")) or "draft",
        "draft_kind": _text(_row_value(row, "draft_kind", "")) or "create",
        "draftKind": _text(_row_value(row, "draft_kind", "")) or "create",
        "base_agent_profile_id": _text(_row_value(row, "base_agent_profile_id", "")),
        "baseAgentProfileId": _text(_row_value(row, "base_agent_profile_id", "")),
        "base_version_id": _text(_row_value(row, "base_version_id", "")),
        "baseVersionId": _text(_row_value(row, "base_version_id", "")),
        "target_agent_profile_id": _text(_row_value(row, "target_agent_profile_id", "")),
        "targetAgentProfileId": _text(_row_value(row, "target_agent_profile_id", "")),
        "source_session_id": _text(_row_value(row, "source_session_id", "")),
        "sourceSessionId": _text(_row_value(row, "source_session_id", "")),
        "source_agent_profile_id": _text(_row_value(row, "source_agent_profile_id", "")),
        "sourceAgentProfileId": _text(_row_value(row, "source_agent_profile_id", "")),
        "source_run_id": _text(_row_value(row, "source_run_id", "")),
        "sourceRunId": _text(_row_value(row, "source_run_id", "")),
        "source_turn_id": _text(_row_value(row, "source_turn_id", "")),
        "sourceTurnId": _text(_row_value(row, "source_turn_id", "")),
        "source_client_message_id": _text(_row_value(row, "source_client_message_id", "")),
        "sourceClientMessageId": _text(_row_value(row, "source_client_message_id", "")),
        "workspace_id": _text(_row_value(row, "workspace_id", "")),
        "workspaceId": _text(_row_value(row, "workspace_id", "")),
        "name": _text(_row_value(row, "name", "")),
        "avatar": _text(_row_value(row, "avatar", "")),
        "description": _text(_row_value(row, "description", "")),
        "category": _text(_row_value(row, "category", "")),
        "tags": _json_loads(_row_value(row, "tags_json", ""), []),
        "architecture_template_id": _text(_row_value(row, "architecture_template_id", "")),
        "architectureTemplateId": _text(_row_value(row, "architecture_template_id", "")),
        "recommended_toolsets": _json_loads(_row_value(row, "recommended_toolsets_json", ""), []),
        "recommendedToolsets": _json_loads(_row_value(row, "recommended_toolsets_json", ""), []),
        "recommended_skills": _json_loads(_row_value(row, "recommended_skills_json", ""), []),
        "recommendedSkills": _json_loads(_row_value(row, "recommended_skills_json", ""), []),
        "skill_creation_plans": _json_loads(_row_value(row, "skill_creation_plans_json", ""), []),
        "skillCreationPlans": _json_loads(_row_value(row, "skill_creation_plans_json", ""), []),
        "missing_capabilities": _json_loads(_row_value(row, "missing_capabilities_json", ""), []),
        "missingCapabilities": _json_loads(_row_value(row, "missing_capabilities_json", ""), []),
        "default_model": _text(_row_value(row, "default_model", "")),
        "defaultModel": _text(_row_value(row, "default_model", "")),
        "default_provider": _text(_row_value(row, "default_provider", "")),
        "defaultProvider": _text(_row_value(row, "default_provider", "")),
        "default_permission_mode": _text(_row_value(row, "default_permission_mode", "")) or "default",
        "defaultPermissionMode": _text(_row_value(row, "default_permission_mode", "")) or "default",
        "files": _json_loads(_row_value(row, "files_json", ""), {}),
        "published_agent_profile_id": _text(_row_value(row, "published_agent_profile_id", "")),
        "publishedAgentProfileId": _text(_row_value(row, "published_agent_profile_id", "")),
        "published_version_id": _text(_row_value(row, "published_version_id", "")),
        "publishedVersionId": _text(_row_value(row, "published_version_id", "")),
        "metadata": _json_loads(_row_value(row, "metadata_json", ""), {}),
        "created_at": created_at,
        "createdAt": _iso_timestamp(created_at),
        "updated_at": updated_at,
        "updatedAt": _iso_timestamp(updated_at),
    }
    if runtime_prepared_at > 0:
        payload.update({
            "runtime_prepared_at": runtime_prepared_at,
            "runtimePreparedAt": _iso_timestamp(runtime_prepared_at),
        })
    if published_at > 0:
        payload.update({
            "published_at": published_at,
            "publishedAt": _iso_timestamp(published_at),
        })
    return payload


__all__ = [
    "AgentProfileRepo",
    "AgentProfileRepoImpl",
    "GrowthSummary",
    "Profile",
    "ProfileSpec",
    "ProfileVersion",
]
