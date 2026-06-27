from __future__ import annotations

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
    """params: {activity_id}; returns {ok: bool}."""
    try:
        activity_id = _required_text(params, "activity_id")
    except ValueError as exc:
        return _invalid_params(rid, str(exc))
    db = _get_db()
    return _ok(rid, {"ok": db.mark_activity_cancelled(activity_id)})


@method("activity.mark_read")
def activity_mark_read(rid, params: dict[str, Any]) -> dict:
    """params: {activity_id}; returns {ok: bool}."""
    try:
        activity_id = _required_text(params, "activity_id")
    except ValueError as exc:
        return _invalid_params(rid, str(exc))
    db = _get_db()
    return _ok(rid, {"ok": db.mark_activity_read(activity_id)})
