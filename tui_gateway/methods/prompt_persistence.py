"""Conversation persistence helpers for prompt-submit execution."""

from __future__ import annotations

from typing import Any, Callable


def persist_prompt_user_turn(
    *,
    sid: str,
    session: dict,
    conversation_session_id: str,
    runtime_scope_key: str,
    run_id: str,
    turn_id: str,
    client_message_id: str,
    text: Any,
    persist_user_message: str,
    user_message_persistence: str,
    attachments: list[dict],
    draft_text: str,
    model: str,
    model_descriptor: dict,
    dovie_product_context: str,
    db_for_stable_session: Callable[[str], Any],
    log_prompt_stage: Callable[..., None],
    logger: Any,
) -> None:
    if session.get("transient"):
        return
    if str(user_message_persistence or "").strip().lower() == "external":
        return
    canonical_session_id = str(conversation_session_id or "").strip()
    if not canonical_session_id or not run_id or not turn_id:
        return
    content = str(persist_user_message or text or "")
    if not content.strip() and not attachments:
        return
    db = db_for_stable_session(canonical_session_id)
    if db is None:
        log_prompt_stage(
            session,
            sid,
            "user-persist-skipped",
            run_id=run_id,
            turn_id=turn_id,
            reason="db-unavailable",
        )
        return
    try:
        if db.sessions.get(canonical_session_id) is None:
            db.sessions.create(canonical_session_id, source="tui", transient=False)
    except Exception as exc:
        logger.warning(
            "prompt.submit user turn session ensure failed "
            "sid=%s conversation_session_id=%s: %s",
            sid,
            canonical_session_id,
            exc,
            exc_info=True,
        )
    metadata: dict[str, Any] = {
        "run_id": run_id,
        "turn_id": turn_id,
        "participant_id": "user",
        "participantId": "user",
        "turn_message_index": 0,
        "persist_message_key": f"run:{run_id}|turn:{turn_id}|idx:0",
        "runtime_scope_key": runtime_scope_key or canonical_session_id,
        "conversation_session_id": canonical_session_id,
        "prompt_submit_owned": True,
    }
    optional_metadata = {
        "client_message_id": client_message_id,
        "attachments": attachments,
        "draft_text": draft_text,
        "model": model,
        "model_descriptor": model_descriptor,
        "dovie_product_context": dovie_product_context,
    }
    metadata.update({key: value for key, value in optional_metadata.items() if value})
    try:
        message_id = db.messages.append(
            session_id=canonical_session_id,
            role="user",
            content=content,
            participant_id="user",
            metadata=metadata,
        )
        log_prompt_stage(
            session,
            sid,
            "user-persisted",
            run_id=run_id,
            turn_id=turn_id,
            message_id=message_id,
            content_len=len(content),
            client_message_id=client_message_id,
        )
    except Exception as exc:
        logger.warning(
            "prompt.submit user turn persistence failed "
            "sid=%s conversation_session_id=%s run_id=%s turn_id=%s: %s",
            sid,
            canonical_session_id,
            run_id,
            turn_id,
            exc,
            exc_info=True,
        )
        log_prompt_stage(
            session,
            sid,
            "user-persist-failed",
            run_id=run_id,
            turn_id=turn_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )


def latest_assistant_message_id_for_turn(
    session_id: str,
    turn_metadata: dict | None,
    *,
    db_for_stable_session: Callable[[str], Any],
    turn_identity: Callable[[dict | None], dict],
    turn_matches: Callable[[dict, dict], bool],
) -> str:
    target = turn_identity(turn_metadata)
    if not session_id or not target:
        return ""
    db = db_for_stable_session(session_id)
    if db is None:
        return ""
    try:
        messages = db.messages.all_as_conversation(
            session_id,
            include_ancestors=False,
            include_storage_metadata=True,
        )
    except Exception:
        return ""
    active_turn: dict = {}
    latest_message_id = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        metadata = (
            message.get("metadata")
            if isinstance(message.get("metadata"), dict)
            else {}
        )
        if role == "user":
            active_turn = turn_identity(metadata)
            continue
        if role != "assistant":
            continue
        message_turn = turn_identity(metadata) or active_turn
        if turn_matches(message_turn, target):
            latest_message_id = str(message.get("message_id") or "").strip()
    return latest_message_id


def commit_scope_summary_after_compression(
    *,
    session: dict,
    history: list[dict],
    db_for_stable_session: Callable[[str], Any],
    logger: Any,
) -> None:
    run_context = session.get("run_context")
    if run_context is None:
        return
    conversation_session_id = str(
        getattr(run_context, "conversation_session_id", "") or ""
    ).strip()
    snapshot_id = str(
        getattr(run_context, "activity_context_snapshot_id", "")
        or getattr(run_context, "context_snapshot_id", "")
        or ""
    ).strip()
    if not conversation_session_id or not snapshot_id:
        return
    try:
        db = db_for_stable_session(conversation_session_id)
        memory_service = (
            getattr(db, "conversation_memory", None) if db is not None else None
        )
        if memory_service is None:
            return
        from hermes_agent.domain.context_compaction import (
            ContextCompactionService,
            ContextScope,
        )

        ContextCompactionService(memory_service).checkpoint(
            ContextScope.from_run_context(run_context),
            history,
        )
    except RuntimeError as exc:
        logger.info(
            "context summary CAS lost conversation=%s participant=%s: %s",
            conversation_session_id,
            str(getattr(run_context, "participant_id", "") or ""),
            exc,
        )
    except Exception:
        logger.warning(
            "context summary commit failed conversation=%s participant=%s",
            conversation_session_id,
            str(getattr(run_context, "participant_id", "") or ""),
            exc_info=True,
        )


__all__ = [
    "commit_scope_summary_after_compression",
    "latest_assistant_message_id_for_turn",
    "persist_prompt_user_turn",
]
