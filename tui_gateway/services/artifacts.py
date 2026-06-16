"""Compatibility facade for artifact gateway services."""

from __future__ import annotations

from tui_gateway.services.artifact_registry import (
    artifact_created_payloads_from_tool_complete,
    artifact_target_paths,
    delete_session_artifacts,
    file_mutation_landed,
    list_artifacts,
    parse_tool_result_dict,
    patch_argument_targets,
    prune_artifacts,
    record_artifacts_from_tool_complete,
    resolve_artifact_path,
)
from tui_gateway.services.workspaces import is_path_inside

__all__ = [
    "artifact_created_payloads_from_tool_complete",
    "artifact_target_paths",
    "delete_session_artifacts",
    "file_mutation_landed",
    "is_path_inside",
    "list_artifacts",
    "parse_tool_result_dict",
    "patch_argument_targets",
    "prune_artifacts",
    "record_artifacts_from_tool_complete",
    "resolve_artifact_path",
]
