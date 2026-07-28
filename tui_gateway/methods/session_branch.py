# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import uuid

from dovie_extension.display_transcript import sanitize_transcript_messages
from tui_gateway.methods.session import (
    _bind_session_workspace,
    _bounded_page_limit,
    _clear_session_context,
    _db_unavailable_error,
    _emit,
    _err,
    _get_db,
    _history_to_messages,
    _init_session,
    _make_agent,
    _message_page_info,
    _new_session_key,
    _normalize_session_cwd,
    _ok,
    _resolve_runtime_session,
    _session_cwd,
    _set_session_context,
    _stored_workspace,
    _workspace_from_params,
    method,
)
from tui_gateway.methods.session_history import (
    _session_active_turn_matches_recall_target,
)


def _branch_point_from_params(params: dict) -> dict:
    raw = params.get("branch_point") or params.get("branchPoint") or {}
    raw = raw if isinstance(raw, dict) else {}
    return {
        "message_id": str(raw.get("message_id") or raw.get("messageId") or "").strip(),
        "turn_id": str(
            raw.get("turn_id")
            or raw.get("turnId")
            or params.get("turn_id")
            or params.get("turnId")
            or ""
        ).strip(),
        "run_id": str(
            raw.get("run_id")
            or raw.get("runId")
            or params.get("run_id")
            or params.get("runId")
            or ""
        ).strip(),
        "client_message_id": str(
            raw.get("client_message_id")
            or raw.get("clientMessageId")
            or params.get("client_message_id")
            or params.get("clientMessageId")
            or ""
        ).strip(),
        "include_assistant_response": bool(
            raw.get("include_assistant_response")
            if "include_assistant_response" in raw
            else raw.get("includeAssistantResponse", True)
        ),
    }


def _branch_source_session(params: dict) -> tuple[str, str, dict | None]:
    requested_source = str(
        params.get("source_session_id")
        or params.get("sourceSessionId")
        or params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or ""
    ).strip()
    requested_runtime = str(params.get("execution_session_id") or params.get("executionSessionId") or "").strip()
    requested_session = str(params.get("session_id") or params.get("sessionId") or "").strip()
    runtime_sid, live_session = _resolve_runtime_session(requested_runtime or requested_source or requested_session)
    source_key = requested_source
    if live_session is not None:
        source_key = source_key or str(live_session.get("session_key") or "")
    source_key = source_key or requested_session
    return source_key, runtime_sid, live_session


@method("session.branch")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    source_key, _runtime_sid, live_session = _branch_source_session(params)
    source_key = str(source_key or "").strip()
    if not source_key:
        return _err(rid, 4006, "source_session_id required")
    found = db.sessions.get(source_key)
    if not found:
        by_title = db.sessions.get_by_title(source_key)
        if by_title:
            source_key = by_title["id"]
            found = by_title
        else:
            return _err(rid, 4007, "source session not found")

    branch_point = _branch_point_from_params(params)
    scope = str(params.get("scope") or ("through_turn" if any(
        branch_point.get(key) for key in ("message_id", "turn_id", "run_id", "client_message_id")
    ) else "full_conversation")).strip() or "through_turn"
    live_target = {
        "turn_id": branch_point.get("turn_id", ""),
        "run_id": branch_point.get("run_id", ""),
        "client_message_id": branch_point.get("client_message_id", ""),
    }
    if live_session is not None and live_session.get("running"):
        if not any(live_target.values()) or _session_active_turn_matches_recall_target(live_session, live_target):
            return _err(rid, 4003, "branch point is not stable")

    requested_title = str(params.get("title") or params.get("name") or "").strip()
    idempotency_key = str(
        params.get("idempotency_key")
        or params.get("idempotencyKey")
        or ""
    ).strip()
    new_key = str(params.get("new_session_id") or params.get("newSessionId") or "").strip() or _new_session_key()
    try:
        branch_result = db.branches.branch_session(
            source_session_id=source_key,
            new_session_id=new_key,
            branch_point=branch_point,
            scope=scope,
            title=requested_title or None,
            idempotency_key=idempotency_key or None,
            branch_origin="user_message_action",
        )
    except ValueError as exc:
        message = str(exc)
        if "idempotency key conflicts" in message:
            return _err(rid, 4091, message)
        if "not stable" in message:
            return _err(rid, 4003, message)
        if "not found" in message:
            return _err(rid, 4002, message)
        if "empty" in message:
            return _err(rid, 4004, message)
        return _err(rid, 5008, f"branch failed: {message}")
    except Exception as exc:
        return _err(rid, 5008, f"branch failed: {exc}")

    conversation_session_id = str(branch_result.get("conversation_session_id") or new_key)
    workspace = None
    cwd = ""
    try:
        if live_session is not None:
            cwd = _session_cwd(live_session)
            workspace = _workspace_from_params(
                {"workspace": params.get("workspace") or live_session.get("workspace") or {}},
                cwd,
            )
        else:
            workspace = _stored_workspace(source_key)
            cwd = str((workspace or {}).get("path") or (workspace or {}).get("cwd") or "")
            cwd = _normalize_session_cwd(cwd)
            workspace = _workspace_from_params(
                {"workspace": params.get("workspace") or workspace or {}},
                cwd,
            )
        workspace = _bind_session_workspace(
            session_id=conversation_session_id,
            cwd=cwd,
            workspace=workspace,
        )
    except Exception:
        workspace = workspace or None

    execution_session_id = ""
    activate = bool(params.get("activate", True))
    if activate:
        execution_session_id = uuid.uuid4().hex[:8]
        try:
            history = db.messages.runtime_as_conversation(
                conversation_session_id,
                include_ancestors=False,
                include_storage_metadata=False,
            )
            tokens = _set_session_context(conversation_session_id, terminal_cwd=cwd)
            try:
                agent = _make_agent(
                    execution_session_id,
                    conversation_session_id,
                    session_id=conversation_session_id,
                    cwd=cwd,
                )
            finally:
                _clear_session_context(tokens)
            _init_session(
                execution_session_id,
                conversation_session_id,
                agent,
                list(history),
                cols=int(params.get("cols") or (live_session or {}).get("cols") or 80),
                cwd=cwd,
                workspace=workspace,
            )
        except Exception as exc:
            return _err(rid, 5000, f"agent init failed on branch: {exc}")

    hydrate = str(params.get("hydrate") or "").strip().lower()
    messages = []
    page_info = {}
    if hydrate == "tail":
        try:
            page = db.messages.page_as_conversation(
                conversation_session_id,
                direction="tail",
                limit=_bounded_page_limit(
                    params.get("message_limit", params.get("messageLimit")),
                    default=50,
                    maximum=200,
                ),
                include_ancestors=False,
            )
            messages = sanitize_transcript_messages(_history_to_messages(page.get("messages") or []))
            page_info = _message_page_info(page.get("pageInfo"))
        except Exception as exc:
            return _err(rid, 5000, f"branch hydrate failed: {exc}")

    payload = {
        **branch_result,
        "session_id": execution_session_id,
        "execution_session_id": execution_session_id,
        "conversation_session_id": conversation_session_id,
        "parent_session_id": source_key,
        "workspace": workspace,
        "messages": messages,
        "pageInfo": page_info,
    }
    _emit("session.branched", execution_session_id or conversation_session_id, payload)
    return _ok(rid, payload)
