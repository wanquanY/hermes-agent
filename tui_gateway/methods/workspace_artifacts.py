# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.artifacts import delete_artifact
from tui_gateway.services.artifacts import list_artifacts
from tui_gateway.services.artifacts import prune_artifacts
from tui_gateway.services.artifacts import register_artifact
from tui_gateway.services.workspace import (
    bind_session_workspace,
    delete_session_workspace_bindings,
    list_session_workspace_bindings,
    list_workspaces,
    normalize_session_cwd,
    session_workspace_binding,
    workspace_for_session,
    workspace_from_params,
)

_server = bind_server_globals(globals())


def _stored_session_id(params: dict) -> str:
    raw = str(params.get("session_id") or "").strip()
    if not raw:
        return ""
    session = _sessions.get(raw)
    if session:
        return str(session.get("session_key") or raw)
    return raw


def _session_ids(params: dict) -> list[str]:
    values = [
        params.get("session_id"),
        params.get("sessionId"),
        params.get("stored_session_id"),
        params.get("storedSessionId"),
    ]
    raw_many = params.get("session_ids") or params.get("sessionIds") or []
    if isinstance(raw_many, list):
        values.extend(raw_many)
    return [value for value in dict.fromkeys(str(item or "").strip() for item in values) if value]


@method("workspace.current")
def _(rid, params: dict) -> dict:
    stored_session_id = _stored_session_id(params)
    if not stored_session_id:
        return _err(rid, 4006, "session_id required")
    workspace = workspace_for_session(stored_session_id)
    if not workspace:
        return _ok(rid, {"workspace": None})
    return _ok(rid, {"workspace": workspace})


@method("workspace.session.current")
def _(rid, params: dict) -> dict:
    stored_session_id = _stored_session_id(params)
    if not stored_session_id:
        return _err(rid, 4006, "session_id required")
    binding = session_workspace_binding(stored_session_id)
    return _ok(rid, {"binding": binding, "workspace": (binding or {}).get("workspace")})


@method("workspace.session.bind")
def _(rid, params: dict) -> dict:
    stored_session_id = _stored_session_id(params)
    if not stored_session_id:
        return _err(rid, 4006, "session_id required")
    raw_workspace = params.get("workspace")
    if not isinstance(raw_workspace, dict):
        raw_workspace = {
            "id": params.get("workspace_id") or params.get("workspaceId"),
            "name": params.get("workspace_name") or params.get("workspaceName") or "workspace",
            "path": params.get("workspace_path") or params.get("workspacePath") or params.get("cwd"),
            "kind": params.get("workspace_kind") or params.get("workspaceKind") or "local",
        }
    try:
        cwd = normalize_session_cwd(params.get("cwd") or raw_workspace.get("path"))
        workspace = workspace_from_params({"workspace": raw_workspace}, cwd)
    except Exception as exc:
        return _err(rid, 4004, str(exc))
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    bind_session_workspace(
        session_id=stored_session_id,
        cwd=cwd,
        workspace=workspace,
        metadata=metadata,
    )
    binding = session_workspace_binding(stored_session_id)
    return _ok(rid, {"binding": binding, "workspace": (binding or {}).get("workspace")})


@method("workspace.session.list")
def _(rid, params: dict) -> dict:
    limit = int(params.get("limit", 200) or 200)
    return _ok(rid, {"bindings": list_session_workspace_bindings(limit=limit)})


@method("workspace.session.delete")
def _(rid, params: dict) -> dict:
    session_ids = _session_ids(params)
    if not session_ids:
        return _err(rid, 4006, "session_id required")
    removed = delete_session_workspace_bindings(session_ids)
    return _ok(rid, {"deleted_count": len(removed), "session_ids": session_ids, "bindings": removed})


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


@method("artifacts.register")
def _(rid, params: dict) -> dict:
    stored_session_id = _stored_session_id(params)
    if not stored_session_id:
        return _err(rid, 4006, "session_id required")
    raw_workspace = params.get("workspace")
    if not isinstance(raw_workspace, dict):
        raw_workspace = {
            "id": params.get("workspace_id") or params.get("workspaceId"),
            "name": params.get("workspace_name") or params.get("workspaceName") or "workspace",
            "path": params.get("workspace_path") or params.get("workspacePath") or params.get("cwd"),
            "kind": params.get("workspace_kind") or params.get("workspaceKind") or "local",
        }
    try:
        artifact = register_artifact(
            session_id=stored_session_id,
            path=str(params.get("path") or params.get("filePath") or params.get("file_path") or ""),
            cwd=str(params.get("cwd") or raw_workspace.get("path") or ""),
            workspace=raw_workspace,
            title=str(params.get("title") or ""),
            mime_type=str(params.get("mime_type") or params.get("mimeType") or ""),
            artifact_id=str(params.get("artifact_id") or params.get("artifactId") or params.get("id") or ""),
            origin=params.get("origin") if isinstance(params.get("origin"), dict) else {},
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5027, f"artifact register failed: {exc}")
    return _ok(rid, {"artifact": artifact})


@method("artifacts.delete")
def _(rid, params: dict) -> dict:
    stored_session_id = _stored_session_id(params)
    if not stored_session_id:
        return _err(rid, 4006, "session_id required")
    try:
        result = delete_artifact(
            session_id=stored_session_id,
            artifact_id=str(params.get("artifact_id") or params.get("artifactId") or params.get("id") or ""),
            path=str(params.get("path") or params.get("filePath") or params.get("file_path") or ""),
            workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""),
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5028, f"artifact delete failed: {exc}")
    return _ok(rid, result)


@method("artifacts.prune")
def _(rid, params: dict) -> dict:
    result = prune_artifacts(
        session_id=_stored_session_id(params),
        workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or "").strip(),
        retention_days=int(params.get("retention_days") or params.get("retentionDays") or 30),
        max_artifacts_per_session=int(
            params.get("max_artifacts_per_session")
            or params.get("maxArtifactsPerSession")
            or 500
        ),
        max_artifacts_per_workspace=int(
            params.get("max_artifacts_per_workspace")
            or params.get("maxArtifactsPerWorkspace")
            or 2000
        ),
    )
    return _ok(rid, result)
