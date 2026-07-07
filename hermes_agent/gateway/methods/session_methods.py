"""Session-domain gateway methods (spec §4.1, §J8)."""

from __future__ import annotations

from typing import Any

from hermes_agent.gateway.auth import requires_permission
from hermes_agent.gateway.error_codes import ErrorCode, MethodError
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.gateway.registry import MethodRegistry
from hermes_agent.repositories import (
    BranchSpec,
    SessionFilter,
    SessionIndexPatch,
    SessionNotFound,
    SessionRepo,
)


def make_method_session_get(repo: SessionRepo):
    """Build a ``session.get`` handler bound to a specific SessionRepo instance."""

    @requires_permission("session.read", read_only=True)
    def method_session_get(params: dict[str, Any], ctx: DispatchContext) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        session = repo.get(session_id)
        if session is None:
            raise MethodError(
                ErrorCode.SESSION_NOT_FOUND,
                f"session {session_id!r} not found",
            )
        return {
            "session_id": session.session_id,
            "source": session.source,
            "title": session.title,
            "display_title": session.display_title,
            "session_kind": session.session_kind,
            "conversation_kind": session.conversation_kind,
            "started_at": session.started_at,
            "updated_at": session.updated_at,
            "ended_at": session.ended_at,
            "parent_session_id": session.parent_session_id,
        }

    return method_session_get


_MAX_LIST_LIMIT = 500


def _session_projection(session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "source": session.source,
        "title": session.title,
        "display_title": session.display_title,
        "session_kind": session.session_kind,
        "conversation_kind": session.conversation_kind,
        "started_at": session.started_at,
        "updated_at": session.updated_at,
        "ended_at": session.ended_at,
        "parent_session_id": session.parent_session_id,
    }


def make_method_session_list(repo: SessionRepo):
    @requires_permission("session.read", read_only=True)
    def method_session_list(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        limit = _int_or_default(params.get("limit"), 100)
        limit = max(1, min(int(limit), _MAX_LIST_LIMIT))
        source_raw = params.get("source")
        kind_raw = params.get("session_kind")
        conv_raw = params.get("conversation_kind")
        source: str | None = str(source_raw) if source_raw else None
        session_kind: str | None = str(kind_raw) if kind_raw else None
        conversation_kind: str | None = str(conv_raw) if conv_raw else None
        include_ended = bool(params.get("include_ended", False))
        sessions = repo.list(
            SessionFilter(
                source=source,
                session_kind=session_kind,
                conversation_kind=conversation_kind,
                include_ended=include_ended,
                limit=limit,
            )
        )
        return {
            "sessions": [_session_projection(s) for s in sessions],
        }

    return method_session_list


def make_method_session_close(repo: SessionRepo):
    @requires_permission("session.write")
    def method_session_close(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        reason = str(params.get("reason") or "user_ended")
        existing = repo.get(session_id)
        if existing is None:
            raise MethodError(
                ErrorCode.SESSION_NOT_FOUND,
                f"session {session_id!r} not found",
            )
        repo.close(session_id, reason)
        updated = repo.get(session_id)
        return {
            "session_id": session_id,
            "reason": reason,
            "ended_at": updated.ended_at if updated is not None else None,
        }

    return method_session_close


def make_method_session_branch(repo: SessionRepo):
    @requires_permission("session.write")
    def method_session_branch(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        source_id = str(params.get("source_id") or params.get("session_id") or "").strip()
        new_session_id = str(params.get("new_session_id") or "").strip()
        branch_from_seq = _int_or_default(params.get("branch_from_seq"), 0)
        if not source_id or not new_session_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "source_id (or session_id) and new_session_id are required",
            )
        try:
            child = repo.branch(
                source_id,
                BranchSpec(
                    new_session_id=new_session_id,
                    branch_from_seq=int(branch_from_seq),
                    title=str(params.get("title") or ""),
                    display_title=str(params.get("display_title") or ""),
                ),
            )
        except SessionNotFound as exc:
            raise MethodError(ErrorCode.SESSION_NOT_FOUND, str(exc)) from exc
        return _session_projection(child)


    return method_session_branch


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def make_method_session_create(repo: SessionRepo):
    from hermes_agent.repositories import SessionSpec

    @requires_permission("session.write")
    def method_session_create(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        source = str(params.get("source") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        if not source:
            raise MethodError(ErrorCode.INVALID_PARAMS, "source is required")
        spec = SessionSpec(
            session_id=session_id,
            source=source,
            title=str(params.get("title") or ""),
            display_title=str(params.get("display_title") or ""),
            display_title_source=str(params.get("display_title_source") or ""),
            session_kind=str(params.get("session_kind") or "hermes_session"),
            conversation_kind=str(params.get("conversation_kind") or "direct"),
            owner_agent_profile_id=str(params.get("owner_agent_profile_id") or ""),
            owner_profile_version_id=str(params.get("owner_profile_version_id") or ""),
            runtime_scope_key=str(params.get("runtime_scope_key") or ""),
            parent_session_id=str(params.get("parent_session_id") or ""),
        )
        session = repo.create(spec)
        return _session_projection(session)

    return method_session_create


# Whitelist of session_index columns writable via the gateway. Anything
# else must go through a repo API — the gateway is not free to update
# arbitrary rows.
_INDEX_WHITELIST = frozenset(
    {
        "title",
        "preview",
        "status",
        "running",
        "waiting_approval",
        "active_run_id",
        "active_runtime_session_id",
        "pending_approval_count",
        "message_count",
        "last_activity",
    }
)


def make_method_session_update_index(repo: SessionRepo):
    @requires_permission("session.write")
    def method_session_update_index(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        if repo.get(session_id) is None:
            raise MethodError(
                ErrorCode.SESSION_NOT_FOUND,
                f"session {session_id!r} not found",
            )

        raw_patch = params.get("patch")
        if not isinstance(raw_patch, dict) or not raw_patch:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "patch is required and must be a non-empty object",
            )

        unknown = set(raw_patch) - _INDEX_WHITELIST
        if unknown:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                f"unsupported session_index fields: {sorted(unknown)!r}",
            )

        patch = SessionIndexPatch(
            title=raw_patch.get("title"),
            preview=raw_patch.get("preview"),
            status=raw_patch.get("status"),
            running=raw_patch.get("running"),
            waiting_approval=raw_patch.get("waiting_approval"),
            active_run_id=raw_patch.get("active_run_id"),
            active_runtime_session_id=raw_patch.get("active_runtime_session_id"),
            pending_approval_count=raw_patch.get("pending_approval_count"),
            message_count=raw_patch.get("message_count"),
            last_activity=raw_patch.get("last_activity"),
        )
        repo.update_index(session_id, patch)
        return {
            "session_id": session_id,
            "applied_fields": sorted(raw_patch.keys()),
        }

    return method_session_update_index


def register(registry: MethodRegistry, repo: SessionRepo) -> None:
    registry.register("session.create", make_method_session_create(repo))
    registry.register("session.get", make_method_session_get(repo))
    registry.register("session.list", make_method_session_list(repo))
    registry.register("session.close", make_method_session_close(repo))
    registry.register("session.branch", make_method_session_branch(repo))
    registry.register("session.update_index", make_method_session_update_index(repo))
