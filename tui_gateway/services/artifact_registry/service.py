"""Artifact registry business service."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from typing import Any

from tui_gateway.services.artifact_registry.domain import ArtifactRecord
from tui_gateway.services.artifact_registry.extractors import artifact_target_paths
from tui_gateway.services.persistence.gateway_store import get_gateway_state_store
from tui_gateway.services.workspaces import (
    bind_session_workspace,
    is_path_inside,
    normalize_session_cwd,
    workspace_from_params,
)


def resolve_artifact_path(raw_path: str, cwd: str) -> str:
    expanded = os.path.expanduser(str(raw_path))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(cwd, expanded))


def _relative_artifact_path(artifact_path: str, workspace_path: str) -> str:
    rel = os.path.relpath(artifact_path, workspace_path)
    return "." if rel == os.curdir else rel


def _artifact_id(workspace_id: str, artifact_path: str) -> str:
    raw = f"{workspace_id}:{os.path.abspath(artifact_path)}"
    return "artifact:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _workspace_payload(workspace: dict[str, Any] | None, cwd: str) -> dict[str, Any]:
    return workspace_from_params({"workspace": dict(workspace or {})}, cwd)


def _artifact_records_from_tool_complete(
    *,
    tool_call_id: str,
    name: str,
    args: dict,
    result: str,
    cwd: str,
    workspace: dict[str, Any] | None,
    origin: dict[str, Any] | None = None,
) -> list[ArtifactRecord]:
    workspace_payload = _workspace_payload(workspace, cwd)
    workspace_path = normalize_session_cwd(workspace_payload["path"])
    records: list[ArtifactRecord] = []

    for raw_path in artifact_target_paths(name, args, result):
        artifact_path = resolve_artifact_path(raw_path, cwd)
        if not is_path_inside(artifact_path, workspace_path):
            continue
        if not os.path.isfile(artifact_path):
            continue
        stat = os.stat(artifact_path)
        mime_type = mimetypes.guess_type(artifact_path)[0] or "application/octet-stream"
        artifact_origin = {
            "event": "tool.complete",
            "tool_id": tool_call_id,
            "tool_name": name,
            **dict(origin or {}),
        }
        records.append(
            ArtifactRecord(
                id=_artifact_id(workspace_payload["id"], artifact_path),
                workspace_id=workspace_payload["id"],
                path=artifact_path,
                relative_path=_relative_artifact_path(artifact_path, workspace_path),
                title=os.path.basename(artifact_path),
                mime_type=mime_type,
                size_bytes=int(stat.st_size),
                workspace=workspace_payload,
                origin=artifact_origin,
            )
        )

    return records


def artifact_created_payloads_from_tool_complete(
    *,
    tool_call_id: str,
    name: str,
    args: dict,
    result: str,
    cwd: str,
    workspace: dict[str, Any] | None,
    origin: dict[str, Any] | None = None,
) -> list[dict]:
    return [
        record.to_payload()
        for record in _artifact_records_from_tool_complete(
            tool_call_id=tool_call_id,
            name=name,
            args=args,
            result=result,
            cwd=normalize_session_cwd(cwd),
            workspace=workspace,
            origin=origin,
        )
    ]


def record_artifacts_from_tool_complete(
    *,
    session_id: str,
    tool_call_id: str,
    name: str,
    args: dict,
    result: str,
    cwd: str,
    workspace: dict[str, Any] | None,
    origin: dict[str, Any] | None = None,
) -> list[dict]:
    cwd = normalize_session_cwd(cwd)
    workspace_payload = _workspace_payload(workspace, cwd)
    persisted_workspace = bind_session_workspace(
        session_id=session_id,
        cwd=cwd,
        workspace=workspace_payload,
    )
    store = get_gateway_state_store()
    payloads: list[dict] = []
    for record in _artifact_records_from_tool_complete(
        tool_call_id=tool_call_id,
        name=name,
        args=args,
        result=result,
        cwd=cwd,
        workspace=persisted_workspace,
        origin=origin,
    ):
        persisted = store.upsert_artifact(
            artifact=record.to_payload(),
            session_id=session_id,
        )
        persisted["workspace"] = persisted_workspace
        payloads.append(persisted)
    return payloads


def list_artifacts(
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return []
    return store.list_artifacts(
        session_id=session_id,
        workspace_id=workspace_id,
        limit=limit,
    )
