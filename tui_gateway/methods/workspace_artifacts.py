# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.artifacts import list_artifacts
from tui_gateway.services.workspace import list_workspaces, workspace_for_session

_server = bind_server_globals(globals())


def _stored_session_id(params: dict) -> str:
    raw = str(params.get("session_id") or "").strip()
    if not raw:
        return ""
    session = _sessions.get(raw)
    if session:
        return str(session.get("session_key") or raw)
    return raw


@method("workspace.current")
def _(rid, params: dict) -> dict:
    stored_session_id = _stored_session_id(params)
    if not stored_session_id:
        return _err(rid, 4006, "session_id required")
    workspace = workspace_for_session(stored_session_id)
    if not workspace:
        return _ok(rid, {"workspace": None})
    return _ok(rid, {"workspace": workspace})


@method("workspace.list")
def _(rid, params: dict) -> dict:
    limit = int(params.get("limit", 200) or 200)
    return _ok(rid, {"workspaces": list_workspaces(limit=limit)})


@method("artifacts.list")
def _(rid, params: dict) -> dict:
    limit = int(params.get("limit", 200) or 200)
    stored_session_id = _stored_session_id(params)
    workspace_id = str(params.get("workspace_id") or "").strip() or None
    if not stored_session_id and not workspace_id:
        session, _err_resp = _sess_nowait(params, rid)
        if session:
            stored_session_id = str(session.get("session_key") or "")
    artifacts = list_artifacts(
        session_id=stored_session_id or None,
        workspace_id=workspace_id,
        limit=limit,
    )
    return _ok(rid, {"artifacts": artifacts})
