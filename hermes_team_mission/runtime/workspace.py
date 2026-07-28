"""Team Mission workspace context ownership.

Hermes owns runtime/session workspace semantics. DoXie may pass the currently
selected local workspace context, but Team Mission creation and execution must
normalize, validate, persist, and bind that context inside Hermes before it is
used by mission state or runtime sessions.
"""

from __future__ import annotations

from typing import Any

from tui_gateway.services.workspace import (
    bind_session_workspace,
    normalize_session_cwd,
    session_workspace_binding,
    workspace_from_params,
)


def _text(value: Any = "") -> str:
    return str(value or "").strip()


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        normalized = _text(value)
        if normalized:
            return normalized
    return ""


def workspace_payload_from_params(params: dict[str, Any]) -> dict[str, Any]:
    workspace = params.get("workspace")
    return workspace if isinstance(workspace, dict) else {}


def workspace_id_from_params(params: dict[str, Any]) -> str:
    workspace = workspace_payload_from_params(params)
    return _first_text(
        params.get("workspace_id"),
        params.get("workspaceId"),
        workspace.get("workspace_id"),
        workspace.get("workspaceId"),
        workspace.get("id"),
    )


def workspace_path_from_params(params: dict[str, Any]) -> str:
    workspace = workspace_payload_from_params(params)
    return _first_text(
        params.get("workspace_path"),
        params.get("workspacePath"),
        workspace.get("workspace_path"),
        workspace.get("workspacePath"),
        workspace.get("path"),
    )


def _binding_workspace(binding: dict[str, Any]) -> dict[str, Any]:
    workspace = _object(binding.get("workspace"))
    if workspace:
        return workspace
    return {
        "id": _text(binding.get("workspace_id") or binding.get("workspaceId")),
        "path": _text(binding.get("workspace_path") or binding.get("workspacePath") or binding.get("cwd")),
        "name": "workspace",
        "kind": "local",
    }


def resolve_team_mission_workspace_context(
    params: dict[str, Any],
    *,
    mission: dict[str, Any] | None = None,
    conversation: dict[str, Any] | None = None,
    session_id: str = "",
    require: bool = True,
) -> dict[str, Any]:
    mission = _object(mission)
    conversation = _object(conversation)
    binding = session_workspace_binding(session_id) if _text(session_id) else None
    binding_workspace = _binding_workspace(binding or {}) if binding else {}
    raw_workspace = workspace_payload_from_params(params)

    workspace_path = _first_text(
        workspace_path_from_params(params),
        mission.get("workspace_path"),
        mission.get("workspacePath"),
        conversation.get("workspace_path"),
        conversation.get("workspacePath"),
        binding_workspace.get("path"),
        binding_workspace.get("workspace_path"),
        binding_workspace.get("workspacePath"),
        (binding or {}).get("cwd"),
    )
    if not workspace_path:
        if require:
            raise ValueError("team mission workspace context required")
        return {
            "workspace_id": "",
            "workspace_path": "",
            "cwd": "",
            "workspace": {},
            "binding": binding,
        }

    cwd = normalize_session_cwd(_first_text(params.get("cwd"), workspace_path))
    workspace = workspace_from_params(
        {
            "workspace": {
                "id": _first_text(
                    workspace_id_from_params(params),
                    mission.get("workspace_id"),
                    mission.get("workspaceId"),
                    conversation.get("workspace_id"),
                    conversation.get("workspaceId"),
                    binding_workspace.get("id"),
                    binding_workspace.get("workspace_id"),
                    binding_workspace.get("workspaceId"),
                ),
                "name": _first_text(
                    raw_workspace.get("name"),
                    raw_workspace.get("workspace_name"),
                    raw_workspace.get("workspaceName"),
                    binding_workspace.get("name"),
                    "workspace",
                ),
                "path": workspace_path,
                "kind": _first_text(
                    raw_workspace.get("kind"),
                    raw_workspace.get("workspace_kind"),
                    raw_workspace.get("workspaceKind"),
                    binding_workspace.get("kind"),
                    "local",
                ),
            }
        },
        cwd,
    )
    return {
        "workspace_id": workspace["id"],
        "workspace_path": workspace["path"],
        "cwd": cwd,
        "workspace": workspace,
        "binding": binding,
    }


def bind_team_mission_session_workspace(
    *,
    session_id: str,
    context: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized_session_id = _text(session_id)
    workspace = _object(context.get("workspace"))
    cwd = _text(context.get("cwd"))
    if not normalized_session_id or not workspace or not cwd:
        return None

    existing = session_workspace_binding(normalized_session_id) or {}
    existing_metadata = _object(existing.get("metadata"))
    bind_session_workspace(
        session_id=normalized_session_id,
        cwd=cwd,
        workspace=workspace,
        metadata={
            **existing_metadata,
            **(metadata if isinstance(metadata, dict) else {}),
        },
    )
    return session_workspace_binding(normalized_session_id)
