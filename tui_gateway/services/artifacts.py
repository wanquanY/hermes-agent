"""Compatibility facade for artifact gateway services."""

from __future__ import annotations

from tui_gateway.services.artifact_registry import (
    WorkspaceArtifactSnapshot,
    ArtifactTarget,
    artifact_created_payloads_from_tool_complete,
    artifact_payloads_from_tool_complete,
    artifact_target_changes,
    artifact_target_paths,
    capture_workspace_artifact_snapshot,
    delete_artifact,
    delete_session_artifacts,
    file_mutation_landed,
    list_artifacts,
    parse_tool_result_dict,
    patch_argument_deleted_targets,
    patch_argument_targets,
    prune_artifacts,
    register_artifact,
    record_artifacts_from_tool_complete,
    resolve_artifact_path,
)
from tui_gateway.services.workspaces import is_path_inside

__all__ = [
    "ArtifactTarget",
    "artifact_created_payloads_from_tool_complete",
    "artifact_payloads_from_tool_complete",
    "artifact_target_changes",
    "artifact_target_paths",
    "capture_workspace_artifact_snapshot",
    "delete_artifact",
    "delete_session_artifacts",
    "file_mutation_landed",
    "is_path_inside",
    "list_artifacts",
    "parse_tool_result_dict",
    "patch_argument_deleted_targets",
    "patch_argument_targets",
    "prune_artifacts",
    "register_artifact",
    "record_artifacts_from_tool_complete",
    "resolve_artifact_path",
    "WorkspaceArtifactSnapshot",
]
