"""Workspace normalization, validation, and persistence."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from tui_gateway.services.persistence.gateway_store import get_gateway_state_store
from tui_gateway.services.workspaces.domain import Workspace


def _text(value: Any = "") -> str:
    return str(value or "").strip()


def _first_text(*values: Any) -> str:
    for value in values:
        normalized = _text(value)
        if normalized:
            return normalized
    return ""


def normalize_session_cwd(value: Any = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = os.getenv("DOVIE_WORKSPACE_ROOT", "") or os.getenv("TERMINAL_CWD", "")
        if not raw:
            if os.getenv("DOVIE_PROCESS_ROLE") == "hermes-worker":
                raise ValueError("Dovie workspace root is not configured")
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
    workspace_path = normalize_session_cwd(_first_text(
        workspace.get("path"),
        workspace.get("workspace_path"),
        workspace.get("workspacePath"),
        cwd,
    ))
    if not is_path_inside(cwd, workspace_path):
        raise ValueError(
            f"cwd must be inside workspace path: cwd={cwd} workspace={workspace_path}"
        )

    explicit_id = _first_text(
        workspace.get("id"),
        workspace.get("workspace_id"),
        workspace.get("workspaceId"),
    )
    model = Workspace(
        id=str(explicit_id or _workspace_id_for_path(workspace_path)),
        name=_first_text(
            workspace.get("name"),
            workspace.get("workspace_name"),
            workspace.get("workspaceName"),
            _workspace_name_for_path(workspace_path),
        ),
        path=workspace_path,
        kind=_first_text(
            workspace.get("kind"),
            workspace.get("workspace_kind"),
            workspace.get("workspaceKind"),
            "local",
        ),
    )
    payload = model.to_payload()
    # Hermes stores this only as a runtime/session cache for artifact lookup and
    # session restoration. Dovie remains the authority for product workspace
    # metadata such as default profile, last-used profile, and user-facing names.
    payload["authority"] = "dovie" if explicit_id else "hermes_runtime_cache"
    payload["runtime_cache"] = True
    return payload


def bind_session_workspace(
    *,
    session_id: str,
    cwd: str,
    workspace: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = get_gateway_state_store()
    persisted = store.upsert_workspace(workspace)
    store.bind_session_workspace(
        session_id=session_id,
        workspace_id=persisted["id"],
        cwd=cwd,
        metadata=metadata,
    )
    return persisted


def _workspace_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    authority = "hermes_runtime_cache" if str(row["id"]).startswith("local:") else "dovie"
    return {
        "id": row["id"],
        "name": row["name"],
        "path": row["path"],
        "kind": row.get("kind") or "",
        "cwd": row.get("cwd") or row["path"],
        "session_id": row["session_id"],
        "authority": authority,
        "runtime_cache": True,
        "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def workspace_for_session(session_id: str) -> dict[str, Any] | None:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return None
    row = store.get_session_workspace(session_id)
    if not row:
        return None
    return _workspace_payload_from_row(row)


def session_workspace_binding(session_id: str) -> dict[str, Any] | None:
    workspace = workspace_for_session(session_id)
    if not workspace:
        return None
    metadata = workspace.get("metadata") if isinstance(workspace.get("metadata"), dict) else {}
    return {
        "session_id": workspace["session_id"],
        "workspace_id": workspace["id"],
        "workspace_path": workspace["path"],
        "cwd": workspace.get("cwd") or workspace["path"],
        "workspace": workspace,
        "metadata": metadata,
        **metadata,
    }


def _explicit_workspace_run_context(params: dict[str, Any]) -> dict[str, Any]:
    raw_workspace = params.get("workspace") if isinstance(params.get("workspace"), dict) else {}
    raw_cwd = _first_text(
        params.get("cwd"),
        raw_workspace.get("cwd"),
        raw_workspace.get("path"),
        raw_workspace.get("workspace_path"),
        raw_workspace.get("workspacePath"),
    )
    if not raw_cwd:
        return {}
    cwd = normalize_session_cwd(raw_cwd)
    workspace = workspace_from_params(params, cwd)
    return {"cwd": cwd, "workspace": workspace, "source": "params"}


def _binding_workspace_run_context(session_id: str) -> dict[str, Any]:
    binding = session_workspace_binding(session_id)
    if not binding:
        return {}
    raw_workspace = binding.get("workspace") if isinstance(binding.get("workspace"), dict) else {}
    raw_cwd = _first_text(
        binding.get("cwd"),
        binding.get("workspace_path"),
        raw_workspace.get("cwd"),
        raw_workspace.get("path"),
    )
    if not raw_cwd:
        return {}
    cwd = normalize_session_cwd(raw_cwd)
    workspace = workspace_from_params({"workspace": raw_workspace}, cwd)
    return {"cwd": cwd, "workspace": workspace, "source": "session_workspace_binding"}


def session_workspace_run_context(
    session_id: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the executable cwd/workspace context for a stored session.

    The run-worker process has its own in-memory ``_sessions`` table, so the
    gateway must carry the stored session workspace contract across the process
    boundary explicitly. Prefer an explicit run payload, then fall back to the
    durable ``workspace.session`` binding written by ``session.create`` /
    ``session.resume``.
    """
    normalized_params = params if isinstance(params, dict) else {}
    explicit = _explicit_workspace_run_context(normalized_params)
    if explicit:
        return explicit
    session_id = _text(session_id)
    if not session_id:
        return {}
    return _binding_workspace_run_context(session_id)


def list_session_workspace_bindings(limit: int = 200) -> list[dict[str, Any]]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return []
    bindings = []
    for row in store.list_session_workspaces(limit=limit):
        workspace = _workspace_payload_from_row(row)
        metadata = workspace.get("metadata") if isinstance(workspace.get("metadata"), dict) else {}
        bindings.append({
            "session_id": workspace["session_id"],
            "workspace_id": workspace["id"],
            "workspace_path": workspace["path"],
            "cwd": workspace.get("cwd") or workspace["path"],
            "workspace": workspace,
            "metadata": metadata,
            **metadata,
        })
    return bindings


def delete_session_workspace_bindings(session_ids: list[str]) -> list[dict[str, Any]]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return []
    removed = []
    for row in store.delete_session_workspaces(session_ids):
        workspace = _workspace_payload_from_row(row)
        metadata = workspace.get("metadata") if isinstance(workspace.get("metadata"), dict) else {}
        removed.append({
            "session_id": workspace["session_id"],
            "workspace_id": workspace["id"],
            "workspace_path": workspace["path"],
            "cwd": workspace.get("cwd") or workspace["path"],
            "workspace": workspace,
            "metadata": metadata,
            **metadata,
        })
    return removed


def list_workspaces(limit: int = 200) -> list[dict[str, Any]]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return []
    return [
        {
            **workspace,
            "authority": "hermes_runtime_cache"
            if str(workspace.get("id") or "").startswith("local:")
            else "dovie",
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
