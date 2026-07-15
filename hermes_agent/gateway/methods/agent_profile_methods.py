"""Agent-profile gateway methods (spec §4.5, §J8)."""

from __future__ import annotations

from typing import Any

from hermes_agent.gateway.auth import requires_permission
from hermes_agent.gateway.error_codes import ErrorCode, MethodError
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.gateway.registry import MethodRegistry
from hermes_agent.repositories import (
    AgentProfileRepo,
    ProfileSpec,
    ProfileVersion,
)


def _profile_projection(profile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "slug": profile.slug,
        "name": profile.name,
        "status": profile.status,
        "hermes_home_path": profile.hermes_home_path,
        "category": profile.category,
        "tags": list(profile.tags),
        "description": profile.description,
        "avatar": profile.avatar,
        "is_system_default": profile.is_system_default,
        "default_model": profile.default_model,
        "current_version_id": profile.current_version_id,
        "current_version_number": profile.current_version_number,
    }


def _growth_projection(summary) -> dict[str, Any]:
    return {
        "profile_id": summary.profile_id,
        "total_runs": summary.total_runs,
        "total_messages": summary.total_messages,
        "total_tokens": summary.total_tokens,
        "growth_score": summary.growth_score,
        "updated_at": summary.updated_at,
    }


def make_method_agent_profile_create(repo: AgentProfileRepo):
    @requires_permission("agent_profile.write")
    def method_agent_profile_create(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        profile_id = str(params.get("profile_id") or "").strip()
        slug = str(params.get("slug") or "").strip()
        name = str(params.get("name") or "").strip()
        home = str(params.get("hermes_home_path") or "").strip()
        if not profile_id or not slug or not name or not home:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "profile_id, slug, name, hermes_home_path all required",
            )
        tags_raw = params.get("tags") or []
        if not isinstance(tags_raw, list):
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "tags must be a list if provided",
            )
        tags = tuple(str(t) for t in tags_raw)
        profile = repo.create_profile(
            ProfileSpec(
                profile_id=profile_id,
                slug=slug,
                name=name,
                hermes_home_path=home,
                status=str(params.get("status") or "active"),
                category=str(params.get("category") or ""),
                tags=tags,
                description=str(params.get("description") or ""),
                avatar=str(params.get("avatar") or ""),
                is_system_default=bool(params.get("is_system_default", False)),
                hermes_profile_name=str(params.get("hermes_profile_name") or ""),
                default_model=str(params.get("default_model") or ""),
            )
        )
        return _profile_projection(profile)

    return method_agent_profile_create


def make_method_agent_profile_get(repo: AgentProfileRepo):
    @requires_permission("agent_profile.read", read_only=True)
    def method_agent_profile_get(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        profile_id = str(params.get("profile_id") or "").strip()
        if not profile_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS, "profile_id is required"
            )
        profile = repo.get(profile_id)
        if profile is None:
            raise MethodError(
                ErrorCode.RUN_NOT_FOUND,  # profile-specific code TBD; 5004 as generic
                f"profile {profile_id!r} not found",
            )
        return _profile_projection(profile)

    return method_agent_profile_get


def make_method_agent_profile_add_version(repo: AgentProfileRepo):
    @requires_permission("agent_profile.write")
    def method_agent_profile_add_version(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        profile_id = str(params.get("profile_id") or "").strip()
        version_id = str(params.get("version_id") or "").strip()
        version_number_raw = params.get("version_number")
        if not profile_id or not version_id or version_number_raw is None:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "profile_id, version_id, version_number all required",
            )
        try:
            version_number = int(version_number_raw)
        except (TypeError, ValueError):
            raise MethodError(
                ErrorCode.INVALID_PARAMS, "version_number must be an integer"
            )
        payload_json = str(params.get("payload_json") or "")
        is_current = bool(params.get("is_current", False))
        repo.add_version(
            profile_id,
            ProfileVersion(
                profile_id=profile_id,
                version_id=version_id,
                version_number=version_number,
                payload_json=payload_json,
                is_current=is_current,
            ),
        )
        return {
            "profile_id": profile_id,
            "version_id": version_id,
            "version_number": version_number,
            "is_current": is_current,
        }

    return method_agent_profile_add_version


def make_method_agent_profile_growth_summary(repo: AgentProfileRepo):
    @requires_permission("agent_profile.read", read_only=True)
    def method_agent_profile_growth_summary(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        profile_id = str(params.get("profile_id") or "").strip()
        if not profile_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS, "profile_id is required"
            )
        summary = repo.get_growth_summary(profile_id)
        return _growth_projection(summary)

    return method_agent_profile_growth_summary


def make_method_agent_profile_list(repo: AgentProfileRepo):
    @requires_permission("agent_profile.read", read_only=True)
    def method_agent_profile_list(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        status_filter = params.get("status")
        limit_raw = params.get("limit") or 100
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise MethodError(
                ErrorCode.INVALID_PARAMS, "limit must be an integer"
            )
        limit = max(1, min(limit, 500))
        include_archived = bool(params.get("include_archived") or params.get("includeArchived"))
        profiles = repo.list(
            status=str(status_filter or ""),
            include_archived=include_archived,
            limit=limit,
        )
        return {"profiles": [_profile_projection(profile) for profile in profiles]}

    return method_agent_profile_list


def register(
    registry: MethodRegistry, repo: AgentProfileRepo, conn_provider=None
) -> None:
    registry.register("agent_profile.create", make_method_agent_profile_create(repo))
    registry.register("agent_profile.get", make_method_agent_profile_get(repo))
    registry.register(
        "agent_profile.add_version", make_method_agent_profile_add_version(repo)
    )
    registry.register(
        "agent_profile.growth_summary",
        make_method_agent_profile_growth_summary(repo),
    )
    registry.register("agent_profile.list", make_method_agent_profile_list(repo))
