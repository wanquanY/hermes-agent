"""Artifact registry service for gateway-produced files."""

from .extractors import (
    ArtifactTarget,
    artifact_target_changes,
    artifact_target_paths,
    file_mutation_landed,
    parse_tool_result_dict,
    patch_argument_deleted_targets,
    patch_argument_targets,
)
from .service import (
    artifact_created_payloads_from_tool_complete,
    artifact_payloads_from_tool_complete,
    delete_artifact,
    delete_session_artifacts,
    list_artifacts,
    prune_artifacts,
    register_artifact,
    record_artifacts_from_tool_complete,
    resolve_artifact_path,
)
from .workspace_diff import (
    WorkspaceArtifactSnapshot,
    capture_workspace_artifact_snapshot,
)

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
    "list_artifacts",
    "prune_artifacts",
    "register_artifact",
    "parse_tool_result_dict",
    "patch_argument_deleted_targets",
    "patch_argument_targets",
    "record_artifacts_from_tool_complete",
    "resolve_artifact_path",
    "WorkspaceArtifactSnapshot",
]
