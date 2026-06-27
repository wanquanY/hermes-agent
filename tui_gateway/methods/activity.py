from __future__ import annotations

import asyncio
from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _required_text(params: dict[str, Any], key: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} required")
    return value


def _invalid_params(rid, message: str) -> dict:
    return _err(rid, -32602, f"invalid params: {message}")


def _run_sync(coro, *, method_name: str):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(f"{method_name} cannot run inside an active event loop")


async def _signal_activity_cancel(activity_id: str, fallback_conversation_id: str) -> bool:
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


@method("activity.cancel")
def activity_cancel(rid, params: dict[str, Any]) -> dict:
    """params: {activity_id}; returns {ok, worker_signaled?, reason?}."""
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


@method("activity.mark_read")
def activity_mark_read(rid, params: dict[str, Any]) -> dict:
    """params: {activity_id}; returns {ok: bool}."""
    try:
        activity_id = _required_text(params, "activity_id")
    except ValueError as exc:
        return _invalid_params(rid, str(exc))
    db = _get_db()
    return _ok(rid, {"ok": db.mark_activity_read(activity_id)})
