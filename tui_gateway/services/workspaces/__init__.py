"""Workspace domain service for the TUI gateway."""

from .service import (
    bind_session_workspace,
    delete_session_workspace_bindings,
    is_path_inside,
    list_session_workspace_bindings,
    list_workspaces,
    normalize_session_cwd,
    session_cwd,
    session_workspace_bindings_by_session_ids,
    session_workspace_binding,
    session_workspace_run_context,
    workspace_for_session,
    workspace_from_params,
)

__all__ = [
    "bind_session_workspace",
    "delete_session_workspace_bindings",
    "is_path_inside",
    "list_session_workspace_bindings",
    "list_workspaces",
    "normalize_session_cwd",
    "session_cwd",
    "session_workspace_bindings_by_session_ids",
    "session_workspace_binding",
    "session_workspace_run_context",
    "workspace_for_session",
    "workspace_from_params",
]
