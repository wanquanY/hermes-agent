"""Message-domain gateway methods (spec §4.3, §J8)."""

from __future__ import annotations

from typing import Any

from hermes_agent.gateway.auth import requires_permission
from hermes_agent.gateway.error_codes import ErrorCode, MethodError
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.gateway.registry import MethodRegistry
from hermes_agent.repositories import (
    MessageRepo,
    MessageSpec,
    PageDirection,
)


_MAX_PAGE_LIMIT = 500
_MAX_FTS_LIMIT = 200


def _message_projection(message) -> dict[str, Any]:
    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "participant_id": message.participant_id,
        "timestamp": message.timestamp,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
        "tool_name": message.tool_name,
        "reasoning": message.reasoning,
        "conversation_message_id": message.conversation_message_id,
        "platform_message_id": message.platform_message_id,
        "metadata": message.metadata,
        "active": message.active,
    }


def make_method_message_append(repo: MessageRepo):
    @requires_permission("message.write")
    def method_message_append(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        role = str(params.get("role") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        if not role:
            raise MethodError(ErrorCode.INVALID_PARAMS, "role is required")
        metadata = params.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "metadata must be an object if provided",
            )
        spec = MessageSpec(
            session_id=session_id,
            role=role,
            content=str(params.get("content") or ""),
            participant_id=str(params.get("participant_id") or ""),
            tool_call_id=str(params.get("tool_call_id") or ""),
            tool_calls=str(params.get("tool_calls") or ""),
            tool_name=str(params.get("tool_name") or ""),
            reasoning=str(params.get("reasoning") or ""),
            conversation_message_id=str(
                params.get("conversation_message_id") or ""
            ),
            platform_message_id=str(params.get("platform_message_id") or ""),
            metadata=metadata or {},
            timestamp=_float_or_default(params.get("timestamp"), 0.0),
        )
        message = repo.append(session_id, spec)
        return _message_projection(message)

    return method_message_append


def make_method_message_get_page(repo: MessageRepo):
    @requires_permission("message.read", read_only=True)
    def method_message_get_page(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        direction_raw = str(params.get("direction") or "tail").lower()
        try:
            direction = PageDirection(direction_raw)
        except ValueError:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                f"direction must be 'head' or 'tail', got {direction_raw!r}",
            )
        limit = _int_or_default(params.get("limit"), 50)
        limit = max(1, min(int(limit), _MAX_PAGE_LIMIT))
        cursor_raw = params.get("cursor_id")
        cursor_id: int | None
        if cursor_raw is None or cursor_raw == "":
            cursor_id = None
        else:
            try:
                cursor_id = int(cursor_raw)
            except (TypeError, ValueError):
                raise MethodError(
                    ErrorCode.INVALID_PARAMS,
                    "cursor_id must be an integer if provided",
                )
        include_ancestors = bool(params.get("include_ancestors", True))
        page = repo.get_page(
            session_id,
            cursor_id=cursor_id,
            direction=direction,
            limit=limit,
            include_ancestors=include_ancestors,
        )
        return {
            "session_id": session_id,
            "messages": [_message_projection(m) for m in page.messages],
            "next_cursor_id": page.next_cursor_id,
            "prev_cursor_id": page.prev_cursor_id,
            "has_more": page.has_more,
        }

    return method_message_get_page


def make_method_message_search_fts(repo: MessageRepo):
    @requires_permission("message.read", read_only=True)
    def method_message_search_fts(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            raise MethodError(ErrorCode.INVALID_PARAMS, "query is required")
        session_id = str(params.get("session_id") or "").strip() or None
        limit = _int_or_default(params.get("limit"), 50)
        limit = max(1, min(int(limit), _MAX_FTS_LIMIT))
        filters: dict[str, Any] = {"limit": limit}
        if session_id:
            filters["session_id"] = session_id
        results = repo.search_fts(query, **filters)
        return {
            "query": query,
            "session_id": session_id,
            "messages": [_message_projection(m) for m in results],
        }

    return method_message_search_fts


def make_method_message_merge_metadata(repo: MessageRepo):
    @requires_permission("message.write")
    def method_message_merge_metadata(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        message_id_raw = params.get("message_id")
        patch = params.get("patch")
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        if message_id_raw is None:
            raise MethodError(ErrorCode.INVALID_PARAMS, "message_id is required")
        try:
            message_id = int(message_id_raw)
        except (TypeError, ValueError):
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "message_id must be an integer",
            )
        if not isinstance(patch, dict):
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "patch must be an object",
            )
        try:
            merged = repo.merge_metadata(session_id, message_id, patch)
        except LookupError as exc:
            raise MethodError(ErrorCode.MESSAGE_NOT_FOUND, str(exc)) from exc
        return _message_projection(merged)

    return method_message_merge_metadata


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def make_method_message_get_by_id(repo: MessageRepo, conn_provider=None):
    """``message.get`` — fetch a single message by primary key.

    ``MessageRepo`` doesn't yet expose a ``get_by_id`` method; we scan the
    ``messages`` table via ``conn_provider`` for that. When conn_provider is
    absent, the method registers but returns METHOD_DISABLED.
    """

    @requires_permission("message.read", read_only=True)
    def method_message_get(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        message_id_raw = params.get("message_id")
        if not session_id or message_id_raw is None:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "session_id and message_id are required",
            )
        try:
            message_id = int(message_id_raw)
        except (TypeError, ValueError):
            raise MethodError(
                ErrorCode.INVALID_PARAMS, "message_id must be an integer"
            )
        if conn_provider is None:
            raise MethodError(
                ErrorCode.METHOD_DISABLED,
                "message.get requires a connection provider (not wired)",
            )
        conn = conn_provider(session_id)
        row = conn.execute(
            """
            SELECT id, session_id, role, content, participant_id, tool_call_id,
                   tool_calls, tool_name, timestamp, reasoning,
                   conversation_message_id, platform_message_id, metadata_json,
                   active
              FROM messages
             WHERE id = ? AND session_id = ?
            """,
            (message_id, session_id),
        ).fetchone()
        if row is None:
            raise MethodError(
                ErrorCode.MESSAGE_NOT_FOUND,
                f"message {message_id} not found in session {session_id!r}",
            )
        import json

        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except json.JSONDecodeError:
            metadata = {}
        return {
            "id": int(row["id"]),
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"] or "",
            "participant_id": row["participant_id"],
            "timestamp": float(row["timestamp"] or 0),
            "tool_call_id": row["tool_call_id"] or "",
            "tool_calls": row["tool_calls"] or "",
            "tool_name": row["tool_name"] or "",
            "reasoning": row["reasoning"] or "",
            "conversation_message_id": row["conversation_message_id"],
            "platform_message_id": row["platform_message_id"] or "",
            "metadata": metadata,
            "active": bool(int(row["active"] or 0)),
        }

    return method_message_get


def register(
    registry: MethodRegistry, repo: MessageRepo, conn_provider=None
) -> None:
    registry.register("message.append", make_method_message_append(repo))
    registry.register("message.get_page", make_method_message_get_page(repo))
    registry.register("message.search_fts", make_method_message_search_fts(repo))
    registry.register(
        "message.merge_metadata", make_method_message_merge_metadata(repo)
    )
    if conn_provider is not None:
        registry.register(
            "message.get", make_method_message_get_by_id(repo, conn_provider)
        )
