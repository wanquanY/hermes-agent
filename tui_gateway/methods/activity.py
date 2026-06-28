# ruff: noqa: F401,F403,F405,F821,ARG001
"""ADR-0001 §Phase 1.B Activity Command RPC entrypoints.

The activity.* command RPCs are a pure control-plane ingress layer: validate
the request, persist an activity_commands intent row, and return immediately.
Command satisfaction, activity table mutation, run_events, and worker spawning
belong to the Phase 1.C reconciler and later migration phases.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from hermes_team_mission.domain.activity import (
    ACTIVITY_COMMAND_KINDS,
    ACTIVITY_KINDS,
)
from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _err(rid, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _run_sync(coro, *, method_name: str):
    """Sync-bridge for async helpers — legacy ``activity.cancel`` path only.

    The Activity Command bus RPCs (``activity.command.*``) are pure sync
    DB writes and don't need this. Preserved for the legacy
    ``activity.cancel`` worker-signaling path until Phase 1.E retires it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(f"{method_name} cannot run inside an active event loop")


async def _signal_activity_cancel(activity_id: str, fallback_conversation_id: str) -> bool:
    """Legacy ``activity.cancel`` worker-signaling path. Retire in Phase 1.E."""
    from tui_gateway.services import worker_runtime

    router = worker_runtime.worker_frame_router()
    lookup = getattr(router, "lookup_activity_run", None)
    info = lookup(activity_id) if callable(lookup) else None
    if info is None:
        return False
    supervisor = worker_runtime.worker_supervisor()
    cancel_run = getattr(supervisor, "cancel_run", None)
    conversation_id = str(getattr(info, "conversation_id", "") or fallback_conversation_id or "")
    if callable(cancel_run):
        return bool(await cancel_run(info.scope_key, conversation_id, info.run_id))
    from tui_gateway.run_worker import RunCancelFrame

    return bool(
        await supervisor.send(
            info.scope_key,
            conversation_id,
            RunCancelFrame(run_id=info.run_id),
        )
    )


def _ok(rid, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _generate_command_id() -> str:
    return f"cmd-{uuid.uuid4().hex}"


def _generate_activity_id(kind: str) -> str:
    return f"act-{kind}-{uuid.uuid4().hex[:16]}"


def _required_text(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} required")
    return value.strip()


def _optional_text(params: dict[str, Any], key: str) -> str | None:
    if key not in params:
        return None
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} required")
    return value.strip()


def _optional_dict(params: dict[str, Any], key: str) -> dict[str, Any] | None:
    if key not in params:
        return None
    value = params.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a dict")
    return value


def _invalid_params(rid, message: str) -> dict[str, Any]:
    return _err(rid, -32602, f"invalid params: {message}")


def _validation_error(rid, message: str) -> dict[str, Any]:
    return _err(rid, 4006, message)


def _db_or_error(rid):
    db = _server._get_db()
    if db is None:
        return None, _err(rid, 5008, "state.db unavailable")
    return db, None


def _activity_command_response(
    rid,
    *,
    activity_id: str,
    command_id: str,
    insert_result: dict[str, Any],
    include_activity_id: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "command_id": command_id,
        "status": "accepted",
    }
    if include_activity_id:
        result["activity_id"] = activity_id
    if not insert_result:
        result["already_existed"] = True
    return _ok(rid, result)


def _insert_activity_command(
    rid,
    *,
    activity_id: str,
    command_id: str,
    kind: str,
    params: dict[str, Any],
    source: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if kind not in ACTIVITY_COMMAND_KINDS:
        return None, _err(
            rid, 5009, f"command kind must be one of {sorted(ACTIVITY_COMMAND_KINDS)}"
        )
    db, err = _db_or_error(rid)
    if err:
        return None, err
    return (
        db.insert_activity_command(
            command_id=command_id,
            activity_id=activity_id,
            kind=kind,
            payload=dict(params),
            metadata={"source": source},
        ),
        None,
    )


@method("activity.create")
def activity_create(rid, params: dict[str, Any]) -> dict[str, Any]:
    """Create a new Activity by persisting a command kind=create."""
    try:
        kind = _required_text(params, "kind")
        if kind not in ACTIVITY_KINDS:
            return _validation_error(
                rid, f"kind must be one of {sorted(ACTIVITY_KINDS)}"
            )
        _required_text(params, "conversation_session_id")
        _required_text(params, "conversation_id")
        activity_id = _optional_text(params, "activity_id") or _generate_activity_id(
            kind
        )
        command_id = _optional_text(params, "command_id") or _generate_command_id()
        _optional_text(params, "parent_activity_id")
        _optional_dict(params, "metadata")
    except ValueError as exc:
        return _validation_error(rid, str(exc))

    inserted, err = _insert_activity_command(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        kind="create",
        params=params,
        source="activity.create",
    )
    if err:
        return err
    return _activity_command_response(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        insert_result=inserted or {},
        include_activity_id=True,
    )


@method("activity.start")
def activity_start(rid, params: dict[str, Any]) -> dict[str, Any]:
    """Start an existing Activity by persisting a command kind=start."""
    try:
        activity_id = _required_text(params, "activity_id")
        command_id = _optional_text(params, "command_id") or _generate_command_id()
        _optional_dict(params, "payload")
    except ValueError as exc:
        return _validation_error(rid, str(exc))

    inserted, err = _insert_activity_command(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        kind="start",
        params=params,
        source="activity.start",
    )
    if err:
        return err
    return _activity_command_response(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        insert_result=inserted or {},
    )


@method("activity.cancel")
def activity_cancel(rid, params: dict) -> dict:
    """params: {activity_id}; returns {ok, worker_signaled?, reason?}.

    Legacy synchronous cancel path — preserved for the Doxie desktop
    frontend (hermes-activities.store.ts:338) which checks ``ok === true``
    to paint the row cancelled. The new Activity Command bus exposes a
    parallel ``activity.command.cancel`` entrypoint (see below); Phase 1.E
    will retire this legacy path once the frontend migrates to the
    command bus + event subscription model.
    """
    try:
        activity_id = _required_text(params, "activity_id")
    except ValueError as exc:
        return _invalid_params(rid, str(exc))
    db = _get_db()
    cancelled_ok = db.mark_activity_cancelled(activity_id)
    if not cancelled_ok:
        return _ok(rid, {"ok": False, "reason": "already_terminal"})
    activity = db.get_activity(activity_id) if callable(getattr(db, "get_activity", None)) else None
    conversation_id = str((activity or {}).get("conversation_id") or "").strip()
    worker_signaled = _run_sync(
        _signal_activity_cancel(activity_id, conversation_id),
        method_name="activity.cancel",
    )
    return _ok(rid, {"ok": True, "worker_signaled": worker_signaled})


@method("activity.command.cancel")
def activity_command_cancel(rid, params: dict[str, Any]) -> dict[str, Any]:
    """ADR-0001 §Phase 1.B Activity Command bus: cancel command.

    Pure command-bus ingress — persists a kind=cancel command to
    activity_commands. Does NOT kill workers, NOT mutate activity status,
    NOT emit run_events. State advancement is the reconciler's
    responsibility (1.C).

    Required params: activity_id (strict; no conversation_id resolution
    in 1.B — that's 1.E's job).
    Optional params: command_id (idempotency key), reason.
    """
    try:
        activity_id = _required_text(params, "activity_id")
        command_id = _optional_text(params, "command_id") or _generate_command_id()
        _optional_text(params, "reason")
    except ValueError as exc:
        return _validation_error(rid, str(exc))

    inserted, err = _insert_activity_command(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        kind="cancel",
        params=params,
        source="activity.command.cancel",
    )
    if err:
        return err
    return _activity_command_response(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        insert_result=inserted or {},
    )


@method("activity.complete")
def activity_complete(rid, params: dict[str, Any]) -> dict[str, Any]:
    """Complete an existing Activity by persisting a command kind=complete."""
    try:
        activity_id = _required_text(params, "activity_id")
        command_id = _optional_text(params, "command_id") or _generate_command_id()
        _optional_dict(params, "result")
    except ValueError as exc:
        return _validation_error(rid, str(exc))

    inserted, err = _insert_activity_command(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        kind="complete",
        params=params,
        source="activity.complete",
    )
    if err:
        return err
    return _activity_command_response(
        rid,
        activity_id=activity_id,
        command_id=command_id,
        insert_result=inserted or {},
    )


@method("activity.list")
def activity_list(rid, params: dict[str, Any]) -> dict:
    """params: {conversation_id, status?, limit?}; returns activity dicts."""
    try:
        conversation_id = _required_text(params, "conversation_id")
    except ValueError as exc:
        return _invalid_params(rid, str(exc))
    db = _get_db()
    return _ok(
        rid,
        db.list_activities(
            conversation_id,
            status=params.get("status"),
            limit=params.get("limit"),
        ),
    )


@method("activity.get")
def activity_get(rid, params: dict[str, Any]) -> dict:
    """params: {activity_id}; returns an activity dict or null."""
    try:
        activity_id = _required_text(params, "activity_id")
    except ValueError as exc:
        return _invalid_params(rid, str(exc))
    db = _get_db()
    return _ok(rid, db.get_activity(activity_id))


@method("activity.mark_read")
def activity_mark_read(rid, params: dict[str, Any]) -> dict:
    """params: {activity_id}; returns {ok: bool}."""
    try:
        activity_id = _required_text(params, "activity_id")
    except ValueError as exc:
        return _invalid_params(rid, str(exc))
    db = _get_db()
    return _ok(rid, {"ok": db.mark_activity_read(activity_id)})
