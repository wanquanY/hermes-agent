# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import hashlib
import json
import os
import uuid

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services import run_control
from tui_gateway.services.run_events import list_filtered_events, list_runtime_events
from tui_gateway.services.run_context_resolver import normalize_run_context_params
from tui_gateway.services.runtime_pool import (
    RuntimeLease,
    RuntimeLeaseError,
    acquire_runtime_lease,
)
from tui_gateway.services.subagent_snapshots import (
    SUBAGENT_SNAPSHOT_EVENT_TYPES,
    build_subagent_run_snapshots,
    compact_subagent_detail_events,
    filter_subagent_events,
)

_server = bind_server_globals(globals())

_RETRYABLE_RUN_STATUSES = frozenset(
    {"cancelled", "canceled", "completed", "complete", "failed", "error", "interrupted"}
)


def _conversation_session_id_from_params(params: dict) -> str:
    return str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or ""
    ).strip()


def _run_db_for_stable_session(conversation_session_id: str):
    stable = str(conversation_session_id or "").strip()
    if stable and _is_control_plane_conversation_session_id(stable):
        return _db_for_stable_session(stable)
    return _get_db()


def _status_filters_from_params(params: dict) -> list[str]:
    raw = params.get("statuses") or params.get("status") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [
        str(status or "").strip()
        for status in raw
        if str(status or "").strip()
    ]


def _bounded_limit(value, default: int = 200, maximum: int = 1000) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _runtime_scope_key_from_params(params: dict, session: dict | None = None) -> str:
    explicit = str(params.get("runtime_scope_key") or params.get("runtimeScopeKey") or "").strip()
    if explicit:
        return explicit
    profile = _profile_context_for_params(params) or ((session or {}).get("profile_context") if isinstance(session, dict) else None)
    if isinstance(profile, dict):
        scope_key = str(profile.get("runtime_scope_key") or profile.get("runtimeScopeKey") or "").strip()
        if scope_key:
            return scope_key
        profile_id = str(profile.get("id") or "").strip()
        if profile_id:
            return f"profile:{profile_id}"
        hermes_home = str(profile.get("hermes_home") or "").strip()
        if hermes_home:
            digest = hashlib.sha1(hermes_home.encode("utf-8")).hexdigest()[:12]
            return f"profile-home:{digest}"
    return "profile:agent-default"


def _mapping(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _run_intent_metadata(params: dict) -> dict:
    """Keep the immutable execution intent required to reproduce a run."""

    metadata = {}
    for field_name in (
        "idempotency_key",
        "retry_of_run_id",
        "model",
        "model_descriptor",
        "dovie_product_context",
        "run_context_json",
        "activity_id",
        "activity_kind",
        "participant_id",
    ):
        value = params.get(field_name)
        if value not in (None, "", [], {}):
            metadata[field_name] = value
    retry_attempt = params.get("retry_attempt")
    if retry_attempt is not None:
        try:
            normalized_retry_attempt = int(retry_attempt)
        except (TypeError, ValueError):
            normalized_retry_attempt = 0
        if normalized_retry_attempt > 0:
            metadata["retry_attempt"] = normalized_retry_attempt
    runtime_scope_key = str(
        params.get("runtime_scope_key") or params.get("runtimeScopeKey") or ""
    ).strip()
    if runtime_scope_key:
        metadata["runtime_scope_key"] = runtime_scope_key
    return metadata


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


def _retry_source_message(db, conversation_session_id: str, source_run_id: str) -> dict | None:
    messages = db.messages.list(conversation_session_id) if db is not None else []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        metadata = _mapping(message.get("metadata"))
        if str(metadata.get("run_id") or "").strip() == source_run_id:
            return {**message, "metadata": metadata}
    return None


def prepare_run_retry(params: dict) -> tuple[dict | None, tuple[int, str] | None]:
    """Resolve an immutable retry plan from Hermes-owned run/message truth.

    This method never starts a worker and never mutates transcript state.  The
    caller passes the returned ``submit`` payload through the existing
    ``run.reserve`` + ``run.submit`` command path, preserving its idempotency
    and failure bookkeeping instead of recreating retry semantics client-side.
    """

    conversation_session_id = _conversation_session_id_from_params(params)
    source_run_id = str(
        params.get("source_run_id") or params.get("sourceRunId") or ""
    ).strip()
    client_run_id = str(
        params.get("client_run_id")
        or params.get("clientRunId")
        or params.get("run_id")
        or params.get("command_id")
        or ""
    ).strip()
    idempotency_key = str(
        params.get("idempotency_key") or params.get("idempotencyKey") or ""
    ).strip()
    if not conversation_session_id:
        return None, (4006, "conversation_session_id required")
    if not source_run_id:
        return None, (4006, "source_run_id required")
    if not client_run_id:
        return None, (4006, "client_run_id required")
    if not idempotency_key:
        return None, (4006, "idempotency_key required")
    if client_run_id == source_run_id:
        return None, (4002, "client_run_id must differ from source_run_id")

    db = _run_db_for_stable_session(conversation_session_id)
    source_run = run_control.get_run(source_run_id, db=db)
    if not isinstance(source_run, dict):
        return None, (4404, "source run not found")
    source_conversation_id = str(
        source_run.get("conversation_session_id") or source_run.get("session_id") or ""
    ).strip()
    if source_conversation_id != conversation_session_id:
        return None, (4404, "source run does not belong to conversation")
    source_status = str(source_run.get("status") or "").strip().lower()
    if source_status not in _RETRYABLE_RUN_STATUSES:
        return None, (4009, f"source run is not retryable: {source_status or 'unknown'}")

    source_metadata = _mapping(source_run.get("metadata"))
    try:
        source_attempt = int(
            source_metadata.get("retry_attempt")
            or source_metadata.get("attempt")
            or 1
        )
    except (TypeError, ValueError):
        source_attempt = 1
    expected_attempt = params.get("expected_attempt", params.get("expectedAttempt"))
    if expected_attempt is not None:
        try:
            normalized_expected_attempt = int(expected_attempt)
        except (TypeError, ValueError):
            return None, (4002, "expected_attempt must be a positive integer")
        if normalized_expected_attempt < 1:
            return None, (4002, "expected_attempt must be a positive integer")
        if normalized_expected_attempt != source_attempt:
            return None, (4409, "retry attempt conflict")

    source_message = _retry_source_message(db, conversation_session_id, source_run_id)
    if source_message is None:
        return None, (4404, "source user message not found")
    message_metadata = _mapping(source_message.get("metadata"))
    text = _message_text(source_message.get("content"))
    attachments = message_metadata.get("attachments")
    attachments = attachments if isinstance(attachments, list) else []
    if not text.strip() and not attachments:
        return None, (4404, "source user message is empty")

    retry_attempt = source_attempt + 1
    runtime_scope_key = str(
        source_run.get("runtime_scope_key")
        or source_metadata.get("runtime_scope_key")
        or message_metadata.get("runtime_scope_key")
        or ""
    ).strip()
    submit = {
        "conversation_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": client_run_id,
        "run_id": client_run_id,
        "turn_id": str(params.get("turn_id") or uuid.uuid4().hex).strip(),
        "client_message_id": str(
            params.get("client_message_id") or f"retry:{client_run_id}"
        ).strip(),
        "idempotency_key": idempotency_key,
        "retry_of_run_id": source_run_id,
        "retry_attempt": retry_attempt,
        "text": text,
        "persist_user_message": text,
        "draft_text": str(message_metadata.get("draft_text") or text),
        "attachments": attachments,
        **({"runtime_scope_key": runtime_scope_key} if runtime_scope_key else {}),
    }
    for field_name in (
        "model",
        "model_descriptor",
        "dovie_product_context",
        "run_context_json",
        "activity_id",
        "activity_kind",
        "participant_id",
    ):
        value = source_metadata.get(field_name, message_metadata.get(field_name))
        if value not in (None, "", [], {}):
            submit[field_name] = value
    return {
        "status": "prepared",
        "conversation_session_id": conversation_session_id,
        "source_run_id": source_run_id,
        "source_attempt": source_attempt,
        "retry_attempt": retry_attempt,
        "submit": submit,
    }, None


def _mark_registered_run_failed(
    *,
    run_id: str,
    conversation_session_id: str,
    runtime_scope_key: str,
    turn_id: str = "",
    message: str = "",
) -> None:
    db = _run_db_for_stable_session(conversation_session_id)
    if db is None or not run_id or not conversation_session_id:
        return
    run_control.terminate_run(
        conversation_session_id=conversation_session_id,
        run_id=run_id,
        turn_id=turn_id,
        runtime_scope_key=runtime_scope_key,
        status="failed",
        message=message,
        db=db,
        owner_transport=current_transport(),
    )


def _runtime_for_run_target(rid, params: dict) -> tuple[str, dict | None, dict | None]:
    target = _conversation_session_id_from_params(params)
    lease = acquire_runtime_lease(
        rid=rid,
        conversation_session_id=target,
        params=params,
        resolve_runtime_session=_resolve_runtime_session,
        resume_runtime_session=_methods["session.resume"],
        session_lookup=lambda sid: _sessions.get(sid),
        transport=current_transport(),
        fallback_transport=_stdio_transport,
        runtime_scope_key=str(
            params.get("runtime_scope_key") or params.get("runtimeScopeKey") or ""
        ),
    )
    if isinstance(lease, RuntimeLeaseError):
        return "", None, lease.response
    if isinstance(lease, RuntimeLease):
        return lease.execution_session_id, lease.session, None
    return "", None, _err(rid, 5000, "runtime lease unavailable")


@method("run.submit")
def _(rid, params: dict) -> dict:
    target = _conversation_session_id_from_params(params)
    run_db = _run_db_for_stable_session(target) if target else _get_db()
    try:
        params = normalize_run_context_params(
            params,
            conversation_session_id=target,
            db=run_db,
        )
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    requested_run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    requested_turn_id = str(params.get("turn_id") or uuid.uuid4().hex).strip()
    requested_scope_key = _runtime_scope_key_from_params(params)
    control_plane_reserved = bool(
        params.get("_control_plane_reserved")
        or params.get("control_plane_reserved")
        or params.get("controlPlaneReserved")
    )
    transient = bool(params.get("transient") or params.get("temporary") or params.get("ephemeral"))
    if target and requested_run_id and not control_plane_reserved and not transient:
        reservation = run_control.create_run_if_session_idle(
            conversation_session_id=target,
            run_id=requested_run_id,
            turn_id=requested_turn_id,
            runtime_scope_key=requested_scope_key,
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                **_run_intent_metadata(params),
            },
            db=run_db,
        )
        conflict = reservation.get("conflict") if isinstance(reservation, dict) else None
        if isinstance(conflict, dict) and conflict:
            metadata = conflict.get("metadata") if isinstance(conflict.get("metadata"), dict) else {}
            logger.warning(
                "[dovie-run-submit] session busy conversation_session_id=%s requested_run_id=%s active_run_id=%s active_status=%s runtime_scope_key=%s execution_session_id=%s gateway_pid=%s gateway_instance_id=%s",
                target,
                requested_run_id,
                conflict.get("run_id") or "",
                conflict.get("status") or "",
                conflict.get("runtime_scope_key") or "",
                conflict.get("execution_session_id") or "",
                metadata.get("gateway_pid") or "",
                metadata.get("gateway_instance_id") or "",
            )
            response = _err(rid, 4009, "session busy")
            response["error"]["data"] = {
                "conversation_session_id": target,
                "active_run_id": conflict.get("run_id") or "",
                "active_turn_id": conflict.get("turn_id") or "",
                "active_status": conflict.get("status") or "",
                "runtime_scope_key": conflict.get("runtime_scope_key") or "",
                "execution_session_id": conflict.get("execution_session_id") or "",
                "run_updated_at": conflict.get("updated_at") or 0,
                "run_started_at": conflict.get("started_at") or 0,
                "metadata": metadata,
                "requested_run_id": requested_run_id,
                "requested_turn_id": requested_turn_id,
                "current_gateway_pid": os.getpid(),
                "current_gateway_instance_id": _GATEWAY_INSTANCE_ID,
            }
            return response
        existing_run = reservation.get("run") if isinstance(reservation, dict) else None
        if isinstance(existing_run, dict) and not reservation.get("created"):
            return _ok(
                rid,
                {
                    "status": existing_run.get("status") or "queued",
                    "run_id": existing_run.get("run_id") or requested_run_id,
                    "turn_id": existing_run.get("turn_id") or requested_turn_id,
                    "conversation_session_id": existing_run.get("conversation_session_id") or existing_run.get("session_id") or target,
                    "runtime_scope_key": existing_run.get("runtime_scope_key") or requested_scope_key,
                },
            )
    sid, session, err = _runtime_for_run_target(rid, params)
    if err:
        if target and requested_run_id and not transient:
            _mark_registered_run_failed(
                run_id=requested_run_id,
                conversation_session_id=target,
                runtime_scope_key=requested_scope_key,
                turn_id=requested_turn_id,
                message=err.get("error", {}).get("message", "runtime target unavailable"),
            )
        return err

    conversation_session_id = str((session or {}).get("session_key") or params.get("conversation_session_id") or sid)
    runtime_scope_key = _runtime_scope_key_from_params(params, session)
    submit_params = {
        **params,
        "session_id": sid,
        "conversation_session_id": conversation_session_id,
        "runtime_scope_key": runtime_scope_key,
        "run_id": requested_run_id,
        "turn_id": requested_turn_id,
        "_run_registry_reserved": True,
    }
    response = _methods["prompt.submit"](rid, submit_params)
    if isinstance(response, dict) and response.get("error") and requested_run_id and not transient:
        _mark_registered_run_failed(
            run_id=requested_run_id,
            conversation_session_id=conversation_session_id,
            runtime_scope_key=runtime_scope_key,
            turn_id=requested_turn_id,
            message=response.get("error", {}).get("message", "prompt submit failed"),
        )
        return response
    result = response.get("result") if isinstance(response, dict) else None
    if isinstance(result, dict):
        run_id = str(result.get("run_id") or params.get("client_run_id") or params.get("run_id") or "").strip()
        turn_id = str(result.get("turn_id") or params.get("turn_id") or "").strip()
        status = str(result.get("status") or "").strip().lower()
        if (
            run_id
            and status not in {"cancelled", "canceled", "failed", "interrupted", "completed", "complete"}
            and not bool((session or {}).get("transient") or transient)
        ):
            run_control.mark_run_started(
                conversation_session_id=conversation_session_id,
                execution_session_id=sid,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=runtime_scope_key,
                metadata={
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                    **_run_intent_metadata(submit_params),
                },
                db=run_db,
            )
        result["runtime_scope_key"] = runtime_scope_key
    return response


@method("run.reserve")
def _(rid, params: dict) -> dict:
    target = _conversation_session_id_from_params(params)
    requested_run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    requested_turn_id = str(params.get("turn_id") or uuid.uuid4().hex).strip()
    requested_scope_key = _runtime_scope_key_from_params(params)
    if not target:
        return _err(rid, 4006, "conversation_session_id required")
    if not requested_run_id:
        return _err(rid, 4006, "run_id required")
    if params.get("transient") or params.get("temporary") or params.get("ephemeral"):
        return _ok(
            rid,
            {
                "status": "queued",
                "run_id": requested_run_id,
                "turn_id": requested_turn_id,
                "conversation_session_id": target,
                "runtime_scope_key": requested_scope_key,
                "created": False,
                "transient": True,
            },
        )
    run_db = _run_db_for_stable_session(target)
    try:
        params = normalize_run_context_params(
            params,
            conversation_session_id=target,
            db=run_db,
        )
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    requested_scope_key = _runtime_scope_key_from_params(params)
    reservation = run_control.create_run_if_session_idle(
        conversation_session_id=target,
        run_id=requested_run_id,
        turn_id=requested_turn_id,
        runtime_scope_key=requested_scope_key,
        metadata={
            "gateway_pid": os.getpid(),
            "gateway_instance_id": _GATEWAY_INSTANCE_ID,
            "reserved_by": "control_plane",
            **_run_intent_metadata(params),
        },
        db=run_db,
    )
    conflict = reservation.get("conflict") if isinstance(reservation, dict) else None
    if isinstance(conflict, dict) and conflict:
        metadata = conflict.get("metadata") if isinstance(conflict.get("metadata"), dict) else {}
        logger.warning(
            "[dovie-run-reserve] session busy conversation_session_id=%s requested_run_id=%s active_run_id=%s active_status=%s runtime_scope_key=%s execution_session_id=%s gateway_pid=%s gateway_instance_id=%s",
            target,
            requested_run_id,
            conflict.get("run_id") or "",
            conflict.get("status") or "",
            conflict.get("runtime_scope_key") or "",
            conflict.get("execution_session_id") or "",
            metadata.get("gateway_pid") or "",
            metadata.get("gateway_instance_id") or "",
        )
        response = _err(rid, 4009, "session busy")
        response["error"]["data"] = {
            "conversation_session_id": target,
            "active_run_id": conflict.get("run_id") or "",
            "active_turn_id": conflict.get("turn_id") or "",
            "active_status": conflict.get("status") or "",
            "runtime_scope_key": conflict.get("runtime_scope_key") or "",
            "execution_session_id": conflict.get("execution_session_id") or "",
            "run_updated_at": conflict.get("updated_at") or 0,
            "run_started_at": conflict.get("started_at") or 0,
            "metadata": metadata,
            "requested_run_id": requested_run_id,
            "requested_turn_id": requested_turn_id,
            "current_gateway_pid": os.getpid(),
            "current_gateway_instance_id": _GATEWAY_INSTANCE_ID,
        }
        return response
    run = reservation.get("run") if isinstance(reservation, dict) else None
    return _ok(
        rid,
        {
            "status": (run or {}).get("status") or "queued",
            "run_id": (run or {}).get("run_id") or requested_run_id,
            "turn_id": (run or {}).get("turn_id") or requested_turn_id,
            "conversation_session_id": (run or {}).get("conversation_session_id") or (run or {}).get("session_id") or target,
            "runtime_scope_key": (run or {}).get("runtime_scope_key") or requested_scope_key,
            "created": bool(reservation.get("created")) if isinstance(reservation, dict) else False,
        },
    )


@method("run.retry.prepare")
def _(rid, params: dict) -> dict:
    prepared, failure = prepare_run_retry(params)
    if failure is not None:
        code, message = failure
        return _err(rid, code, message)
    return _ok(rid, prepared)


@method("run.fail")
def _(rid, params: dict) -> dict:
    run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    conversation_session_id = _conversation_session_id_from_params(params)
    if not run_id or not conversation_session_id:
        return _err(rid, 4006, "run_id and conversation_session_id required")
    run_db = _run_db_for_stable_session(conversation_session_id)
    state = run_control.get_run(run_id, db=run_db) or {}
    turn_id = str(params.get("turn_id") or params.get("turnId") or state.get("turn_id") or "").strip()
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or state.get("runtime_scope_key")
        or ""
    ).strip()
    message = str(params.get("message") or params.get("error") or "run failed").strip()
    event = run_control.terminate_run(
        conversation_session_id=conversation_session_id,
        run_id=run_id,
        execution_session_id=str(state.get("execution_session_id") or ""),
        runtime_scope_key=runtime_scope_key,
        turn_id=turn_id,
        status="failed",
        message=message,
        db=run_db,
        owner_transport=current_transport(),
    )
    return _ok(
        rid,
        {
            "status": "failed",
            "run_id": run_id,
            "turn_id": turn_id,
            "seq": int((event or {}).get("seq") or 0),
        },
    )


@method("run.status")
def _(rid, params: dict) -> dict:
    run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    if run_id:
        db = _get_db()
        state = run_control.get_run(run_id, db=db)
        if state is None:
            return _err(rid, 4040, "run not found")
        conversation_session_id = str(
            state.get("conversation_session_id")
            or state.get("session_id")
            or ""
        ).strip()
        session_state = {}
        if str(state.get("status") or "") in run_control.ACTIVE_RUN_STATUSES:
            if conversation_session_id:
                session_state = run_control.session_status(
                    conversation_session_id,
                    db=db,
                    current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
                )
                state = run_control.get_run(run_id, db=db) or state
        run_status = str(state.get("status") or "").strip()
        result = dict(session_state) if isinstance(session_state, dict) else {}
        result["run"] = state
        result["run_id"] = str(state.get("run_id") or run_id)
        result["conversation_session_id"] = conversation_session_id
        result["status"] = run_status
        result["last_event_seq"] = int(
            result.get("last_event_seq")
            or state.get("last_seq")
            or 0
        )
        result.setdefault("running", run_status in run_control.ACTIVE_RUN_STATUSES)
        result.setdefault("active_run_id", result["run_id"] if result["running"] else "")
        result.setdefault("active_turn_id", str(state.get("turn_id") or ""))
        result.setdefault("runtime_scope_key", str(state.get("runtime_scope_key") or ""))
        result.setdefault("run_started_at", float(state.get("started_at") or 0))
        result.setdefault("run_updated_at", float(state.get("updated_at") or 0))
        return _ok(rid, result)

    conversation_session_id = str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or ""
    ).strip()
    if not conversation_session_id:
        return _err(rid, 4006, "run_id or conversation_session_id required")
    db = _run_db_for_stable_session(conversation_session_id)
    result = run_control.session_status(
        conversation_session_id,
        db=db,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    )
    result = dict(result) if isinstance(result, dict) else {}
    active_run_id = str(result.get("active_run_id") or "").strip()
    active_run = run_control.get_run(active_run_id, db=db) if active_run_id else None
    active_status = str((active_run or {}).get("status") or "").strip()
    result["conversation_session_id"] = conversation_session_id
    result["run_id"] = active_run_id
    result["status"] = active_status or ("running" if result.get("running") else "idle")
    result["last_event_seq"] = int(
        result.get("last_event_seq")
        or (active_run or {}).get("last_seq")
        or 0
    )
    return _ok(rid, result)


@method("run.list")
def _(rid, params: dict) -> dict:
    conversation_session_id = _conversation_session_id_from_params(params)
    runtime_scope_key = str(params.get("runtime_scope_key") or params.get("runtimeScopeKey") or "").strip()
    statuses = _status_filters_from_params(params)
    if not conversation_session_id and not runtime_scope_key and not statuses:
        return _err(rid, 4006, "conversation_session_id, runtime_scope_key, or status required")
    db = _run_db_for_stable_session(conversation_session_id) if conversation_session_id else _get_db()
    return _ok(
        rid,
        {
            "runs": run_control.list_runs(
                conversation_session_id,
                db=db,
                runtime_scope_key=runtime_scope_key,
                statuses=statuses,
                limit=_bounded_limit(params.get("limit")),
            )
        },
    )


@method("run.cancel")
def _(rid, params: dict) -> dict:
    run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    conversation_session_id = _conversation_session_id_from_params(params)
    if not run_id and not conversation_session_id:
        return _err(rid, 4006, "run_id or conversation_session_id required")
    execution_session_id = str(params.get("execution_session_id") or params.get("executionSessionId") or "").strip()
    if not execution_session_id and conversation_session_id:
        resolved_sid, _session = _resolve_runtime_session(conversation_session_id)
        execution_session_id = resolved_sid or conversation_session_id
    interrupt_params = {
        **params,
        "session_id": execution_session_id or conversation_session_id or params.get("session_id") or "",
        "conversation_session_id": conversation_session_id,
        "run_id": run_id,
        "completion_status": "cancelled",
    }
    response = _methods["session.interrupt"](rid, interrupt_params)
    if isinstance(response, dict) and isinstance(response.get("result"), dict):
        response["result"]["status"] = "cancelled"
    if response.get("error") and run_id:
        db = _run_db_for_stable_session(conversation_session_id) if conversation_session_id else _get_db()
        if db is not None:
            try:
                state = run_control.get_run(run_id, db=db) or {}
                session_id = str(state.get("conversation_session_id") or state.get("session_id") or conversation_session_id)
                event = run_control.terminate_run(
                    conversation_session_id=session_id,
                    run_id=run_id,
                    execution_session_id=str(state.get("execution_session_id") or state.get("session_id") or ""),
                    runtime_scope_key=str(state.get("runtime_scope_key") or session_id),
                    turn_id=str(state.get("turn_id") or ""),
                    status="cancelled",
                    message="cancelled without live runtime",
                    db=db,
                    owner_transport=current_transport(),
                )
                return _ok(
                    rid,
                    {
                        "status": "cancelled",
                        "run_id": run_id,
                        "turn_id": str(state.get("turn_id") or ""),
                        "seq": int((event or {}).get("seq") or 0),
                    },
                )
            except Exception:
                pass
    return response


@method("events.subscribe")
def _(rid, params: dict) -> dict:
    conversation_session_id = str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or ""
    ).strip()
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    runtime_scope_key = str(
        params.get("runtime_scope_key") or params.get("runtimeScopeKey") or ""
    ).strip()
    db = _run_db_for_stable_session(conversation_session_id)
    subscription_id, replay = run_control.subscribe_session_with_id(
        conversation_session_id=conversation_session_id,
        transport=current_transport(),
        after_seq=after_seq,
        active_only=bool(params.get("active_only") or params.get("activeOnly")),
        runtime_scope_key=runtime_scope_key,
        db=db,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    )
    last_seq = max([int(event.get("seq") or 0) for event in replay], default=after_seq)
    return _ok(
        rid,
        {
            "conversation_session_id": conversation_session_id,
            "subscription_id": subscription_id,
            "events": replay,
            "last_event_seq": last_seq,
        },
    )


@method("run.events")
def _(rid, params: dict) -> dict:
    conversation_session_id = str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or ""
    ).strip()
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    runtime_scope_key = str(
        params.get("runtime_scope_key") or params.get("runtimeScopeKey") or ""
    ).strip()
    run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    db = _run_db_for_stable_session(conversation_session_id)
    run_control.session_status(
        conversation_session_id,
        db=db,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    )
    events = list_runtime_events(
        db,
        conversation_session_id,
        after_seq=after_seq,
        active_only=bool(params.get("active_only") or params.get("activeOnly")),
        runtime_scope_key=runtime_scope_key,
        run_id=run_id,
        limit=_bounded_limit(params.get("limit"), default=2000, maximum=5000),
    )
    return _ok(
        rid,
        {
            "conversation_session_id": conversation_session_id,
            "events": events,
            "last_event_seq": max([int(event.get("seq") or 0) for event in events], default=after_seq),
        },
    )


def _list_filtered_run_events(
    *,
    conversation_session_id: str,
    after_seq: int = 0,
    runtime_scope_key: str = "",
    event_type_prefix: str = "",
    event_types: tuple[str, ...] | list[str] | None = None,
    payload_contains: str = "",
    limit: int = 2000,
) -> list[dict]:
    db = _run_db_for_stable_session(conversation_session_id) if conversation_session_id else _get_db()
    if db is None:
        return []
    events = list_filtered_events(
        db,
        conversation_session_id,
        after_seq=after_seq,
        runtime_scope_key=runtime_scope_key,
        event_type_prefix=event_type_prefix,
        event_types=event_types,
        payload_contains=payload_contains,
        limit=limit,
    )
    return events


@method("subagent.runs.list")
def _(rid, params: dict) -> dict:
    conversation_session_id = _conversation_session_id_from_params(params)
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    runtime_scope_key = str(
        params.get("runtime_scope_key") or params.get("runtimeScopeKey") or ""
    ).strip()
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    limit = _bounded_limit(params.get("limit"), default=5000, maximum=20000)
    events = _list_filtered_run_events(
        conversation_session_id=conversation_session_id,
        after_seq=after_seq,
        runtime_scope_key=runtime_scope_key,
        event_types=SUBAGENT_SNAPSHOT_EVENT_TYPES,
        limit=limit,
    )
    runs = build_subagent_run_snapshots(events, conversation_session_id=conversation_session_id)
    return _ok(
        rid,
        {
            "conversation_session_id": conversation_session_id,
            "runs": runs,
            "last_event_seq": max([int(event.get("seq") or 0) for event in events], default=after_seq),
        },
    )


@method("subagent.events.list")
def _(rid, params: dict) -> dict:
    conversation_session_id = _conversation_session_id_from_params(params)
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    subagent_id = str(params.get("subagent_id") or params.get("subagentId") or "").strip()
    if not subagent_id:
        return _err(rid, 4006, "subagent_id required")
    runtime_scope_key = str(
        params.get("runtime_scope_key") or params.get("runtimeScopeKey") or ""
    ).strip()
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    limit = _bounded_limit(params.get("limit"), default=5000, maximum=20000)
    events = _list_filtered_run_events(
        conversation_session_id=conversation_session_id,
        after_seq=after_seq,
        runtime_scope_key=runtime_scope_key,
        event_type_prefix="subagent.",
        payload_contains=subagent_id,
        limit=limit,
    )
    events = filter_subagent_events(events, subagent_id)
    events = compact_subagent_detail_events(events)
    return _ok(
        rid,
        {
            "conversation_session_id": conversation_session_id,
            "subagent_id": subagent_id,
            "events": events,
            "last_event_seq": max([int(event.get("seq") or 0) for event in events], default=after_seq),
        },
    )


@method("events.unsubscribe")
def _(rid, params: dict) -> dict:
    subscription_id = str(params.get("subscription_id") or params.get("subscriptionId") or "").strip()
    conversation_session_id = _conversation_session_id_from_params(params)
    removed = run_control.unsubscribe_session(
        subscription_id=subscription_id,
        conversation_session_id=conversation_session_id,
        transport=current_transport(),
    )
    return _ok(
        rid,
        {
            "removed": removed,
            "subscription_id": subscription_id,
            "conversation_session_id": conversation_session_id,
        },
    )


@method("events.prune")
def _(rid, params: dict) -> dict:
    conversation_session_id = _conversation_session_id_from_params(params)
    db = _run_db_for_stable_session(conversation_session_id) if conversation_session_id else _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5006)
    result = db.runs.retention.prune(
        session_id=conversation_session_id,
        retention_days=int(params.get("retention_days") or params.get("retentionDays") or 14),
        max_events_per_session=int(params.get("max_events_per_session") or params.get("maxEventsPerSession") or 5000),
    )
    return _ok(rid, result)


@method("events.compact")
def _(rid, params: dict) -> dict:
    conversation_session_id = _conversation_session_id_from_params(params)
    db = _run_db_for_stable_session(conversation_session_id) if conversation_session_id else _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5006)
    result = db.run_event_maintenance.compact(
        session_id=conversation_session_id,
        vacuum=bool(params.get("vacuum")),
    )
    return _ok(rid, result)
