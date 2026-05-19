# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import hashlib
import os
import uuid

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services import run_control
from tui_gateway.services.runtime_pool import (
    RuntimeLease,
    RuntimeLeaseError,
    acquire_runtime_lease,
)

_server = bind_server_globals(globals())


def _stored_session_id_from_params(params: dict) -> str:
    return str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or ""
    ).strip()


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
            version_id = str(
                profile.get("agent_profile_version_id")
                or profile.get("agentProfileVersionId")
                or ""
            ).strip()
            return f"profile:{profile_id}:version:{version_id}" if version_id else f"profile:{profile_id}"
        hermes_home = str(profile.get("hermes_home") or "").strip()
        if hermes_home:
            digest = hashlib.sha1(hermes_home.encode("utf-8")).hexdigest()[:12]
            return f"profile-home:{digest}"
    return "profile:agent-default"


def _mark_registered_run_failed(
    *,
    run_id: str,
    stored_session_id: str,
    runtime_scope_key: str,
    turn_id: str = "",
    message: str = "",
) -> None:
    db = _get_db()
    if db is None or not run_id or not stored_session_id:
        return
    run_control.publish_run_terminal_event(
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        runtime_scope_key=runtime_scope_key,
        status="failed",
        message=message,
        db=db,
        owner_transport=current_transport(),
    )


def _runtime_for_run_target(rid, params: dict) -> tuple[str, dict | None, dict | None]:
    target = _stored_session_id_from_params(params)
    lease = acquire_runtime_lease(
        rid=rid,
        stored_session_id=target,
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
        return lease.runtime_session_id, lease.session, None
    return "", None, _err(rid, 5000, "runtime lease unavailable")


@method("run.submit")
def _(rid, params: dict) -> dict:
    target = _stored_session_id_from_params(params)
    requested_run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    requested_turn_id = str(params.get("turn_id") or uuid.uuid4().hex).strip()
    requested_scope_key = _runtime_scope_key_from_params(params)
    control_plane_reserved = bool(
        params.get("_control_plane_reserved")
        or params.get("control_plane_reserved")
        or params.get("controlPlaneReserved")
    )
    if target and requested_run_id and not control_plane_reserved:
        reservation = run_control.create_run_if_session_idle(
            stored_session_id=target,
            run_id=requested_run_id,
            turn_id=requested_turn_id,
            runtime_scope_key=requested_scope_key,
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": _GATEWAY_INSTANCE_ID,
            },
            db=_get_db(),
        )
        conflict = reservation.get("conflict") if isinstance(reservation, dict) else None
        if isinstance(conflict, dict) and conflict:
            response = _err(rid, 4009, "session busy")
            response["error"]["data"] = {
                "stored_session_id": target,
                "active_run_id": conflict.get("run_id") or "",
                "active_turn_id": conflict.get("turn_id") or "",
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
                    "stored_session_id": existing_run.get("stored_session_id") or existing_run.get("session_id") or target,
                    "runtime_scope_key": existing_run.get("runtime_scope_key") or requested_scope_key,
                },
            )
    sid, session, err = _runtime_for_run_target(rid, params)
    if err:
        if target and requested_run_id:
            _mark_registered_run_failed(
                run_id=requested_run_id,
                stored_session_id=target,
                runtime_scope_key=requested_scope_key,
                turn_id=requested_turn_id,
                message=err.get("error", {}).get("message", "runtime target unavailable"),
            )
        return err

    stable_session_id = str((session or {}).get("session_key") or params.get("stored_session_id") or sid)
    runtime_scope_key = _runtime_scope_key_from_params(params, session)
    submit_params = {
        **params,
        "session_id": sid,
        "stored_session_id": stable_session_id,
        "runtime_scope_key": runtime_scope_key,
        "run_id": requested_run_id,
        "turn_id": requested_turn_id,
        "_run_registry_reserved": True,
    }
    response = _methods["prompt.submit"](rid, submit_params)
    if isinstance(response, dict) and response.get("error") and requested_run_id:
        _mark_registered_run_failed(
            run_id=requested_run_id,
            stored_session_id=stable_session_id,
            runtime_scope_key=runtime_scope_key,
            turn_id=requested_turn_id,
            message=response.get("error", {}).get("message", "prompt submit failed"),
        )
        return response
    result = response.get("result") if isinstance(response, dict) else None
    if isinstance(result, dict):
        run_id = str(result.get("run_id") or params.get("client_run_id") or params.get("run_id") or "").strip()
        turn_id = str(result.get("turn_id") or params.get("turn_id") or "").strip()
        if run_id:
            run_control.mark_run_started(
                stored_session_id=stable_session_id,
                runtime_session_id=sid,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=runtime_scope_key,
                metadata={
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                },
                db=_get_db(),
            )
        result["runtime_scope_key"] = runtime_scope_key
    return response


@method("run.reserve")
def _(rid, params: dict) -> dict:
    target = _stored_session_id_from_params(params)
    requested_run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    requested_turn_id = str(params.get("turn_id") or uuid.uuid4().hex).strip()
    requested_scope_key = _runtime_scope_key_from_params(params)
    if not target:
        return _err(rid, 4006, "stored_session_id required")
    if not requested_run_id:
        return _err(rid, 4006, "run_id required")
    reservation = run_control.create_run_if_session_idle(
        stored_session_id=target,
        run_id=requested_run_id,
        turn_id=requested_turn_id,
        runtime_scope_key=requested_scope_key,
        metadata={
            "gateway_pid": os.getpid(),
            "gateway_instance_id": _GATEWAY_INSTANCE_ID,
            "reserved_by": "control_plane",
        },
        db=_get_db(),
    )
    conflict = reservation.get("conflict") if isinstance(reservation, dict) else None
    if isinstance(conflict, dict) and conflict:
        response = _err(rid, 4009, "session busy")
        response["error"]["data"] = {
            "stored_session_id": target,
            "active_run_id": conflict.get("run_id") or "",
            "active_turn_id": conflict.get("turn_id") or "",
        }
        return response
    run = reservation.get("run") if isinstance(reservation, dict) else None
    return _ok(
        rid,
        {
            "status": (run or {}).get("status") or "queued",
            "run_id": (run or {}).get("run_id") or requested_run_id,
            "turn_id": (run or {}).get("turn_id") or requested_turn_id,
            "stored_session_id": (run or {}).get("stored_session_id") or (run or {}).get("session_id") or target,
            "runtime_scope_key": (run or {}).get("runtime_scope_key") or requested_scope_key,
            "created": bool(reservation.get("created")) if isinstance(reservation, dict) else False,
        },
    )


@method("run.fail")
def _(rid, params: dict) -> dict:
    run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    stable_session_id = _stored_session_id_from_params(params)
    if not run_id or not stable_session_id:
        return _err(rid, 4006, "run_id and stored_session_id required")
    state = run_control.get_run(run_id, db=_get_db()) or {}
    turn_id = str(params.get("turn_id") or params.get("turnId") or state.get("turn_id") or "").strip()
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or state.get("runtime_scope_key")
        or ""
    ).strip()
    message = str(params.get("message") or params.get("error") or "run failed").strip()
    event = run_control.publish_run_terminal_event(
        stored_session_id=stable_session_id,
        run_id=run_id,
        runtime_session_id=str(state.get("runtime_session_id") or ""),
        runtime_scope_key=runtime_scope_key,
        turn_id=turn_id,
        status="failed",
        message=message,
        db=_get_db(),
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
        state = run_control.get_run(run_id, db=_get_db())
        if state is None:
            return _err(rid, 4040, "run not found")
        return _ok(rid, {"run": state})

    stable_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or ""
    ).strip()
    if not stable_session_id:
        return _err(rid, 4006, "run_id or stored_session_id required")
    return _ok(rid, run_control.session_status(stable_session_id, db=_get_db()))


@method("run.list")
def _(rid, params: dict) -> dict:
    stable_session_id = _stored_session_id_from_params(params)
    runtime_scope_key = str(params.get("runtime_scope_key") or params.get("runtimeScopeKey") or "").strip()
    statuses = _status_filters_from_params(params)
    if not stable_session_id and not runtime_scope_key and not statuses:
        return _err(rid, 4006, "stored_session_id, runtime_scope_key, or status required")
    return _ok(
        rid,
        {
            "runs": run_control.list_runs(
                stable_session_id,
                db=_get_db(),
                runtime_scope_key=runtime_scope_key,
                statuses=statuses,
                limit=_bounded_limit(params.get("limit")),
            )
        },
    )


@method("run.cancel")
def _(rid, params: dict) -> dict:
    run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    stable_session_id = _stored_session_id_from_params(params)
    if not run_id and not stable_session_id:
        return _err(rid, 4006, "run_id or stored_session_id required")
    interrupt_params = {
        **params,
        "session_id": stable_session_id or params.get("session_id") or "",
        "run_id": run_id,
        "completion_status": "cancelled",
    }
    response = _methods["session.interrupt"](rid, interrupt_params)
    if isinstance(response, dict) and isinstance(response.get("result"), dict):
        response["result"]["status"] = "cancelled"
    if response.get("error") and run_id:
        db = _get_db()
        if db is not None:
            try:
                state = run_control.get_run(run_id, db=db) or {}
                session_id = str(state.get("stored_session_id") or state.get("session_id") or stable_session_id)
                event = run_control.publish_run_terminal_event(
                    stored_session_id=session_id,
                    run_id=run_id,
                    runtime_session_id=str(state.get("runtime_session_id") or state.get("session_id") or ""),
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
    stable_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or ""
    ).strip()
    if not stable_session_id:
        return _err(rid, 4006, "stored_session_id required")
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    subscription_id, replay = run_control.subscribe_session_with_id(
        stored_session_id=stable_session_id,
        transport=current_transport(),
        after_seq=after_seq,
        active_only=bool(params.get("active_only") or params.get("activeOnly")),
        db=_get_db(),
    )
    return _ok(
        rid,
        {
            "stored_session_id": stable_session_id,
            "subscription_id": subscription_id,
            "events": replay,
            "last_event_seq": max([int(event.get("seq") or 0) for event in replay], default=after_seq),
        },
    )


@method("events.unsubscribe")
def _(rid, params: dict) -> dict:
    subscription_id = str(params.get("subscription_id") or params.get("subscriptionId") or "").strip()
    stable_session_id = _stored_session_id_from_params(params)
    removed = run_control.unsubscribe_session(
        subscription_id=subscription_id,
        stored_session_id=stable_session_id,
        transport=current_transport(),
    )
    return _ok(
        rid,
        {
            "removed": removed,
            "subscription_id": subscription_id,
            "stored_session_id": stable_session_id,
        },
    )


@method("events.prune")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5006)
    stable_session_id = _stored_session_id_from_params(params)
    result = db.prune_run_events(
        session_id=stable_session_id,
        retention_days=int(params.get("retention_days") or params.get("retentionDays") or 14),
        max_events_per_session=int(params.get("max_events_per_session") or params.get("maxEventsPerSession") or 5000),
    )
    return _ok(rid, result)
