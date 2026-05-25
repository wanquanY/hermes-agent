"""Workspace normalization, validation, and persistence."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from tui_gateway.services.persistence.gateway_store import get_gateway_state_store
from tui_gateway.services.workspaces.domain import Workspace


def normalize_session_cwd(value: Any = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = os.getenv("DOXIE_WORKSPACE_ROOT", "") or os.getenv("TERMINAL_CWD", "")
        if not raw:
            if os.getenv("DOXIE_PROCESS_ROLE") == "hermes-worker":
                raise ValueError("Doxie workspace root is not configured")
            raw = os.getcwd()
    cwd = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(cwd):
        raise ValueError(f"cwd does not exist or is not a directory: {cwd}")
    return cwd


def is_path_inside(child_path: str, parent_path: str) -> bool:
    try:
        child = os.path.abspath(child_path)
        parent = os.path.abspath(parent_path)
        return os.path.commonpath([child, parent]) == parent
    except Exception:
        return False


def _workspace_id_for_path(path: str) -> str:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:20]
    return f"local:{digest}"


def _workspace_name_for_path(path: str) -> str:
    return os.path.basename(os.path.normpath(path)) or "workspace"


def workspace_from_params(params: dict, cwd: str) -> dict:
    raw = params.get("workspace")
    workspace = raw if isinstance(raw, dict) else {}
    workspace_path = normalize_session_cwd(workspace.get("path") or cwd)
    if not is_path_inside(cwd, workspace_path):
        raise ValueError(
            f"cwd must be inside workspace path: cwd={cwd} workspace={workspace_path}"
        )

    explicit_id = workspace.get("id") or workspace.get("workspace_id")
    model = Workspace(
        id=str(explicit_id or _workspace_id_for_path(workspace_path)),
        name=str(workspace.get("name") or _workspace_name_for_path(workspace_path)),
        path=workspace_path,
        kind=str(workspace.get("kind") or "local"),
    )
    payload = model.to_payload()
    # Hermes stores this only as a runtime/session cache for artifact lookup and
    # session restoration. Doxie remains the authority for product workspace
    # metadata such as default profile, last-used profile, and user-facing names.
    payload["authority"] = "doxie" if explicit_id else "hermes_runtime_cache"
    payload["runtime_cache"] = True
    return payload


def bind_session_workspace(
    *,
    session_id: str,
    cwd: str,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    store = get_gateway_state_store()
    persisted = store.upsert_workspace(workspace)
    store.bind_session_workspace(
        session_id=session_id,
        workspace_id=persisted["id"],
        cwd=cwd,
    )
    return persisted


def workspace_for_session(session_id: str) -> dict[str, Any] | None:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return None
    row = store.get_session_workspace(session_id)
    if not row:
        return None
    authority = "hermes_runtime_cache" if str(row["id"]).startswith("local:") else "doxie"
    return {
        "id": row["id"],
        "name": row["name"],
        "path": row["path"],
        "kind": row.get("kind") or "",
        "cwd": row.get("cwd") or row["path"],
        "session_id": row["session_id"],
        "authority": authority,
        "runtime_cache": True,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def list_workspaces(limit: int = 200) -> list[dict[str, Any]]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return []
    return [
        {
            **workspace,
            "authority": "hermes_runtime_cache"
            if str(workspace.get("id") or "").startswith("local:")
            else "doxie",
            "runtime_cache": True,
        }
        for workspace in store.list_workspaces(limit=limit)
    ]


def session_cwd(session: dict | None = None) -> str:
    if session and session.get("cwd"):
        return str(session["cwd"])
    try:
        from gateway.session_context import get_session_env

        return normalize_session_cwd(get_session_env("TERMINAL_CWD", ""))
    except Exception:
        return normalize_session_cwd()
