"""Artifact registry service for gateway-produced files."""

from .extractors import (
    artifact_target_paths,
    file_mutation_landed,
    parse_tool_result_dict,
    patch_argument_targets,
)
from .service import (
    artifact_created_payloads_from_tool_complete,
    delete_artifact,
    delete_session_artifacts,
    list_artifacts,
    prune_artifacts,
    register_artifact,
    record_artifacts_from_tool_complete,
    resolve_artifact_path,
)

__all__ = [
    "artifact_created_payloads_from_tool_complete",
    "artifact_target_paths",
    "delete_artifact",
    "delete_session_artifacts",
    "file_mutation_landed",
    "list_artifacts",
    "prune_artifacts",
    "register_artifact",
    "parse_tool_result_dict",
    "patch_argument_targets",
    "record_artifacts_from_tool_complete",
    "resolve_artifact_path",
]
