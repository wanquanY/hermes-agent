# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from dovie_extension.display_transcript import sanitize_transcript_messages
from hermes_agent.read_models.message_history import MessagePageQuery
from tui_gateway.methods import session as _session_methods
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.methods.session import (
    _bounded_page_limit,
    _decode_page_cursor,
    _encode_page_cursor,
    _message_page_info,
    _requested_runtime_scope_key,
)
from tui_gateway.services.message_history import (
    load_conversation_history,
    message_history_read_model_for_db,
    message_repository_for_db,
)
from tui_gateway.services.run_events import list_runtime_events, list_tool_events

_server = bind_server_globals(globals())


def _text(value) -> str:
    return str(value or "").strip()


def _get_db():
    return _session_methods._get_db()


def _session_repo_for_db(db):
    return _session_methods._session_repo_for_db(db)


def _resolve_session_row_id(rid, db, target: str) -> tuple[str, dict | None]:
    repo = _session_repo_for_db(db)
    if repo is None:
        return "", _err(rid, 5000, "session repository unavailable")
    found = repo.get(target)
    if not found:
        found = repo.get_by_title(target)
    if not found:
        return "", _err(rid, 4007, "session not found")
    return found.session_id, None


def _coerce_int(value, *, default: int = 0) -> int:
    """Best-effort int coercion for cursor params; falls back to ``default``."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _max_seq(events: list) -> int:
    """Return the highest ``seq`` among ``events`` (0 when empty)."""
    best = 0
    for event in events or []:
        try:
            seq = int((event or {}).get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq > best:
            best = seq
    return best


@method("session.history")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    history = list(session.get("history", []))
    db = _get_db()
    if db is not None and session.get("session_key"):
        try:
            history = load_conversation_history(
                db,
                session["session_key"],
                include_ancestors=True,
            )
        except Exception:
            pass
    return _ok(
        rid,
        {
            "count": len(history),
            "messages": sanitize_transcript_messages(_history_to_messages(history)),
        },
    )


@method("session.messages")
def _(rid, params: dict) -> dict:
    target = str(params.get("session_id") or "").strip()
    if not target:
        return _err(rid, 4006, "session_id required")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5000)
    target, resolve_err = _resolve_session_row_id(rid, db, target)
    if resolve_err:
        return resolve_err
    cursor = _decode_page_cursor(params.get("cursor"))
    cursor_id = cursor.get("id")
    try:
        cursor_id = int(cursor_id) if cursor_id is not None else None
    except (TypeError, ValueError):
        cursor_id = None
    message_history = message_history_read_model_for_db(db)
    if message_history is None:
        return _err(rid, 5000, "message history read model unavailable")
    try:
        page = message_history.page_as_conversation(
            target,
            MessagePageQuery(
                direction=str(params.get("direction") or "tail"),
                cursor_id=cursor_id,
                limit=_bounded_page_limit(params.get("limit"), default=50, maximum=200),
                include_ancestors=bool(
                    params.get("include_ancestors", params.get("includeAncestors", True))
                ),
            ),
        )
    except Exception as exc:
        return _err(rid, 5000, f"messages page failed: {exc}")
    activity_id = str(params.get("activity_id") or params.get("activityId") or "").strip()
    include_run_events = bool(params.get("include_run_events", params.get("includeRunEvents", False)))
    # PR-2 §4.2: run_events cursor.  after_seq is a forward cursor (only
    # events with seq > after_seq); before_seq is a backward cursor for
    # loading earlier history (only events with seq < before_seq).  The DB
    # layer's list_run_events only supports after_seq natively, so before_seq
    # is applied as a post-filter here.  These two are independent of the
    # message-dimension cursor_id pagination above.
    after_seq = _coerce_int(params.get("after_seq", params.get("afterSeq")), default=0)
    before_seq = _coerce_int(params.get("before_seq", params.get("beforeSeq")), default=0)
    cursor_active = after_seq > 0 or before_seq > 0
    run_events = []
    run_events_warning = ""
    if include_run_events:
        try:
            run_events = list_runtime_events(
                db,
                target,
                after_seq=after_seq,
                runtime_scope_key=_requested_runtime_scope_key(params),
                activity_id=activity_id,
                limit=_bounded_page_limit(
                    params.get("run_events_limit", params.get("runEventsLimit")),
                    default=2000,
                    maximum=5000,
                ),
            )
            # PR-2 §4.2: before_seq is not a DB-level filter — apply it
            # as a Python post-filter so callers can page backwards.
            if before_seq > 0:
                run_events = [e for e in run_events if int((e or {}).get("seq") or 0) < before_seq]
            # PR-2 §4.2: when no cursor is supplied the legacy behavior
            # returns the full event set.  Mark it deprecated so callers
            # migrate to the cursor path.
            if not cursor_active:
                run_events_warning = (
                    "include_run_events without after_seq/before_seq returns the "
                    "full event window and is deprecated; pass after_seq for "
                    "cursor-based pagination."
                )
        except Exception as exc:
            return _err(rid, 5000, f"run event page failed: {exc}")
    run_events_max_seq = _max_seq(run_events)
    include_tool_events = bool(params.get("include_tool_events", params.get("includeToolEvents", False)))
    tool_events = []
    if include_tool_events:
        try:
            tool_events = list_tool_events(
                db,
                target,
                after_seq=after_seq,
                limit=_bounded_page_limit(
                    params.get("tool_events_limit", params.get("toolEventsLimit")),
                    default=2000,
                    maximum=5000,
                ),
            )
        except Exception as exc:
            return _err(rid, 5000, f"tool event page failed: {exc}")
    raw_messages = _history_to_messages(page.get("messages") or [])
    sanitized_messages = sanitize_transcript_messages(raw_messages)
    page_info = _message_page_info(page.get("pageInfo"))
    branch_info = db.get_session_branch_info(target) if hasattr(db, "get_session_branch_info") else None
    result = {
        "session_id": target,
        "messages": sanitized_messages,
        "toolEvents": tool_events,
        "runEvents": run_events,
        "maxSeq": run_events_max_seq,
        "pageInfo": page_info,
        "branchInfo": branch_info,
    }
    if run_events_warning:
        result["runEventsWarning"] = run_events_warning
    return _ok(rid, result)


@method("session.events")
def _(rid, params: dict) -> dict:
    """PR-2 §4.2: lightweight run_events cursor reader.

    Returns only run_events (no messages), supporting forward pagination via
    ``after_seq``.  ``maxSeq`` is the highest seq in the returned window and
    serves as the next-page cursor; ``hasMore`` is true when the DB returned
    a full page (i.e. the limit was the binding constraint).
    """
    target = str(params.get("session_id") or "").strip()
    if not target:
        return _err(rid, 4006, "session_id required")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5000)
    target, resolve_err = _resolve_session_row_id(rid, db, target)
    if resolve_err:
        return resolve_err
    after_seq = _coerce_int(params.get("after_seq", params.get("afterSeq")), default=0)
    limit = _bounded_page_limit(
        params.get("limit"),
        default=200,
        maximum=5000,
    )
    try:
        events = list_runtime_events(
            db,
            target,
            after_seq=after_seq,
            runtime_scope_key=_requested_runtime_scope_key(params),
            activity_id=str(params.get("activity_id") or params.get("activityId") or "").strip(),
            limit=limit,
        )
    except Exception as exc:
        return _err(rid, 5000, f"events page failed: {exc}")
    max_seq = _max_seq(events)
    has_more = len(events) >= limit
    return _ok(
        rid,
        {
            "session_id": target,
            "events": events,
            "maxSeq": max_seq,
            "hasMore": has_more,
        },
    )


@method("session.message_metadata.merge")
def _(rid, params: dict) -> dict:
    target = str(params.get("session_id") or "").strip()
    if not target:
        return _err(rid, 4006, "session_id required")
    metadata = params.get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        return _err(rid, 4006, "metadata object required")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5000)
    target, resolve_err = _resolve_session_row_id(rid, db, target)
    if resolve_err:
        return resolve_err
    message_repo = message_repository_for_db(db)
    if message_repo is None:
        return _err(rid, 5000, "message metadata merge is not available")
    try:
        message = message_repo.merge_metadata(
            target,
            metadata,
            message_id=params.get("message_id") or params.get("messageId"),
            role=params.get("role"),
            run_id=params.get("run_id") or params.get("runId"),
            turn_id=params.get("turn_id") or params.get("turnId"),
            client_message_id=params.get("client_message_id") or params.get("clientMessageId"),
        )
    except Exception as exc:
        return _err(rid, 5000, f"message metadata merge failed: {exc}")
    if not message:
        return _err(rid, 4007, "message not found")
    messages = sanitize_transcript_messages(_history_to_messages([message]))
    return _ok(
        rid,
        {
            "session_id": target,
            "message": messages[0] if messages else message,
        },
    )


@method("session.undo")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    # Reject during an in-flight turn.  If we mutated history while
    # the agent thread is running, prompt.submit's post-run history
    # write would either clobber the undo (version matches) or
    # silently drop the agent's output (version mismatch, see below).
    # Neither is what the user wants — make them /interrupt first.
    if session.get("running"):
        return _err(
            rid, 4009, "session busy — /interrupt the current turn before /undo"
        )
    removed = 0
    with session["history_lock"]:
        history = session.get("history", [])
        while history and history[-1].get("role") in {"assistant", "tool"}:
            history.pop()
            removed += 1
        if history and history[-1].get("role") == "user":
            history.pop()
            removed += 1
        if removed:
            session["history_version"] = int(session.get("history_version", 0)) + 1
    return _ok(rid, {"removed": removed})


def _message_turn_id(message: dict) -> str:
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("turn_id") or "")
    return ""


def _turn_recall_target(params: dict) -> dict[str, str]:
    return {
        "turn_id": str(params.get("turn_id") or params.get("turnId") or "").strip(),
        "run_id": str(params.get("run_id") or params.get("runId") or "").strip(),
        "client_message_id": str(
            params.get("client_message_id")
            or params.get("clientMessageId")
            or ""
        ).strip(),
    }


def _message_matches_recall_target(message: dict, target: dict[str, str]) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in ("turn_id", "run_id", "client_message_id"):
        expected = str(target.get(key) or "").strip()
        if expected and str(metadata.get(key) or "").strip() == expected:
            return True
    return False


def _pending_turn_matches_recall_target(pending_turn: dict | None, target: dict[str, str]) -> bool:
    if not isinstance(pending_turn, dict):
        return False
    for key in ("turn_id", "run_id", "client_message_id"):
        expected = str(target.get(key) or "").strip()
        if expected and str(pending_turn.get(key) or "").strip() == expected:
            return True
    return False


def _session_active_turn_matches_recall_target(session: dict, target: dict[str, str]) -> bool:
    active_turn_id = str(session.get("active_turn_id") or "").strip()
    active_run_id = str(session.get("active_run_id") or "").strip()
    if active_turn_id and active_turn_id == str(target.get("turn_id") or "").strip():
        return True
    if active_run_id and active_run_id == str(target.get("run_id") or "").strip():
        return True
    return _pending_turn_matches_recall_target(session.get("pending_turn"), target)


def _draft_from_turn_message(message: dict | None, pending_turn: dict | None = None) -> dict:
    metadata = message.get("metadata") if isinstance(message, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    pending_turn = pending_turn if isinstance(pending_turn, dict) else {}
    text = (
        metadata.get("draft_text")
        or pending_turn.get("draft_text")
        or (message or {}).get("content")
        or pending_turn.get("text")
        or ""
    )
    return {
        "turnId": str(metadata.get("turn_id") or pending_turn.get("turn_id") or ""),
        "text": str(text or ""),
        "attachments": (
            metadata.get("attachments")
            if isinstance(metadata.get("attachments"), list)
            else pending_turn.get("attachments") if isinstance(pending_turn.get("attachments"), list) else []
        ),
        "model": str(metadata.get("model") or pending_turn.get("model") or ""),
    }


def _recall_event_payload(
    *,
    turn_id: str,
    removed_messages: int,
    draft: dict,
    messages: list[dict],
    run_id: str = "",
) -> dict:
    payload = {
        "turn_id": turn_id,
        "removed_messages": removed_messages,
        "draft": draft,
        "messages": messages,
    }
    normalized_run_id = str(run_id or "").strip()
    if normalized_run_id:
        payload["run_id"] = normalized_run_id
    return payload


def _rewrite_live_and_persisted_history(session: dict, history: list[dict]) -> None:
    session_key = str(session.get("session_key") or "")
    db = _get_db()
    if db is not None and session_key:
        message_repo = message_repository_for_db(db)
        if message_repo is not None:
            message_repo.replace_conversation(session_key, history)
    session["history"] = history
    session["history_version"] = int(session.get("history_version", 0)) + 1
    agent = session.get("agent")
    if agent is not None:
        try:
            agent._session_messages = history
        except Exception:
            pass
        try:
            agent._last_flushed_db_idx = len(history)
        except Exception:
            pass


def _recall_turn_from_history(
    history: list[dict],
    target: dict[str, str],
    pending_turn: dict | None = None,
) -> tuple[list[dict], dict, int] | None:
    target_idx = None
    for idx, message in enumerate(history):
        if _message_matches_recall_target(message, target):
            target_idx = idx
            break
    if target_idx is None:
        return None

    remove_end = len(history)
    for idx in range(target_idx + 1, len(history)):
        message = history[idx]
        if isinstance(message, dict) and message.get("role") == "user":
            remove_end = idx
            break
    target_message = history[target_idx]
    draft = _draft_from_turn_message(target_message, pending_turn)
    next_history = history[:target_idx] + history[remove_end:]
    return next_history, draft, remove_end - target_idx


def _load_stored_history_for_rewrite(db, session_key: str) -> list[dict]:
    return load_conversation_history(
        db,
        session_key,
        include_ancestors=False,
        include_storage_metadata=True,
    )


def _recall_stored_turn(rid, sid: str, target: dict[str, str]) -> dict | None:
    turn_id = str(target.get("turn_id") or "")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5036)
    session_key = sid
    session_key, resolve_err = _resolve_session_row_id(rid, db, session_key)
    if resolve_err:
        if resolve_err.get("error", {}).get("code") == 4007:
            return None
        return resolve_err
    try:
        history = _load_stored_history_for_rewrite(db, session_key)
        recalled = _recall_turn_from_history(list(history or []), target)
        if recalled is None:
            return _err(rid, 4019, "turn not found or already recalled")
        next_history, draft, removed = recalled
        message_repo = message_repository_for_db(db)
        if message_repo is None:
            return _err(rid, 5036, "message repository unavailable")
        message_repo.replace_conversation(session_key, next_history)
        messages = sanitize_transcript_messages(_history_to_messages(next_history))
    except Exception as exc:
        return _err(rid, 5036, f"recall failed: {exc}")

    _emit("session.recalled", sid, _recall_event_payload(
        turn_id=turn_id,
        removed_messages=removed,
        draft=draft,
        messages=messages,
    ))
    return _ok(rid, {
        "status": "recalled",
        "session_id": sid,
        "conversation_session_id": session_key,
        "turn_id": turn_id,
        "interrupted": False,
        "removed_messages": removed,
        "draft": draft,
        "messages": messages,
        "memory_retract": {
            "status": "unsupported",
            "warnings": ["Memory provider turn-level retraction is not implemented yet."],
        },
    })


@method("session.recall_turn")
def _(rid, params: dict) -> dict:
    sid = str(params.get("session_id") or "").strip()
    target = _turn_recall_target(params)
    turn_id = str(target.get("turn_id") or "")
    if not turn_id:
        return _err(rid, 4006, "turn_id required")
    runtime_sid, live_session = _resolve_runtime_session(sid)
    if live_session is None:
        stored_result = _recall_stored_turn(rid, sid, target)
        if stored_result is not None:
            return stored_result
    session, err = _sess(params, rid)
    if err:
        return err
    sid = str(params.get("session_id") or runtime_sid or sid)

    interrupted = False
    agent_to_interrupt = None
    with session["history_lock"]:
        active_turn_id = str(session.get("active_turn_id") or "")
        active_run_id = str(session.get("active_run_id") or "").strip()
        running_target_matches = _session_active_turn_matches_recall_target(session, target)
        if session.get("running") and active_turn_id and not running_target_matches:
            return _err(rid, 4009, "session busy with a different turn")
        if session.get("running") and running_target_matches:
            interrupted = True
            session["interrupted_run_id"] = active_run_id
            session["interrupted_turn_id"] = turn_id
            session["interrupt_seq"] = int(session.get("interrupt_seq") or 0) + 1
            session.setdefault("recalled_turn_ids", set()).add(turn_id)
            agent_to_interrupt = session.get("agent")

    if agent_to_interrupt is not None and hasattr(agent_to_interrupt, "interrupt"):
        try:
            agent_to_interrupt.interrupt()
        except Exception:
            pass

    with session["history_lock"]:
        history = list(session.get("history") or [])
        pending_turn = session.get("pending_turn")
        recalled = _recall_turn_from_history(history, target, pending_turn)
        if recalled is None:
            if _pending_turn_matches_recall_target(pending_turn, target):
                draft = _draft_from_turn_message(None, pending_turn)
                recall_run_id = active_run_id or str((pending_turn or {}).get("run_id") or "").strip()
                session.setdefault("recalled_turn_ids", set()).add(turn_id)
                session["running"] = False
                session["active_run_id"] = None
                session["active_turn_id"] = None
                session["pending_turn"] = None
                session["run_updated_at"] = time.time()
                messages = sanitize_transcript_messages(_history_to_messages(history))
                _emit("session.recalled", sid, _recall_event_payload(
                    turn_id=turn_id,
                    removed_messages=0,
                    draft=draft,
                    messages=messages,
                    run_id=recall_run_id,
                ))
                _emit("message.complete", sid, {"text": "", "status": "interrupted", "turn_id": turn_id})
                return _ok(rid, {
                    "status": "recalled",
                    "session_id": sid,
                    "conversation_session_id": str(session.get("session_key") or ""),
                    "turn_id": turn_id,
                    "interrupted": interrupted,
                    "removed_messages": 0,
                    "draft": draft,
                    "messages": messages,
                    "memory_retract": {
                        "status": "unsupported",
                        "warnings": ["Memory provider turn-level retraction is not implemented yet."],
                    },
                })
            return _err(rid, 4019, "turn not found or already recalled")

        next_history, draft, removed = recalled
        _rewrite_live_and_persisted_history(session, next_history)
        session.setdefault("recalled_turn_ids", set()).add(turn_id)
        recall_run_id = ""
        if (
            running_target_matches
            or active_turn_id == turn_id
            or str(session.get("active_turn_id") or "") == turn_id
        ):
            recall_run_id = active_run_id
            session["running"] = False
            session["active_run_id"] = None
            session["active_turn_id"] = None
            session["pending_turn"] = None
            session["run_updated_at"] = time.time()
        messages = sanitize_transcript_messages(_history_to_messages(next_history))

    _emit("session.recalled", sid, _recall_event_payload(
        turn_id=turn_id,
        removed_messages=removed,
        draft=draft,
        messages=messages,
        run_id=recall_run_id,
    ))
    if interrupted:
        _emit("message.complete", sid, {"text": "", "status": "interrupted", "turn_id": turn_id})
    return _ok(rid, {
        "status": "recalled",
        "session_id": sid,
        "conversation_session_id": str(session.get("session_key") or ""),
        "turn_id": turn_id,
        "interrupted": interrupted,
        "removed_messages": removed,
        "draft": draft,
        "messages": messages,
        "memory_retract": {
            "status": "unsupported",
            "warnings": ["Memory provider turn-level retraction is not implemented yet."],
        },
    })
