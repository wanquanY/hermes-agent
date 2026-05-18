"""Compatibility facade for workspace gateway services."""

from __future__ import annotations

from tui_gateway.services.workspaces import (
    bind_session_workspace,
    is_path_inside,
    list_workspaces,
    normalize_session_cwd,
    session_cwd,
    workspace_for_session,
    workspace_from_params,
)

__all__ = [
    "bind_session_workspace",
    "is_path_inside",
    "list_workspaces",
    "normalize_session_cwd",
    "session_cwd",
    "workspace_for_session",
    "workspace_from_params",
]
