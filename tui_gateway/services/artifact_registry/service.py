"""Artifact registry business service."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from typing import Any

from tui_gateway.services.artifact_registry.domain import ArtifactRecord
from tui_gateway.services.artifact_registry.extractors import (
    ArtifactTarget,
    artifact_target_changes,
    artifact_target_paths,
)
from tui_gateway.services.artifact_registry.workspace_diff import (
    WorkspaceArtifactSnapshot,
    changed_workspace_artifacts,
)
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


def _artifact_kind(mime_type: str) -> str:
    mime_type = str(mime_type or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("text/") or mime_type in {"application/json", "application/xml"}:
        return "text"
    return "file"


def _artifact_payload_contract(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    mime_type = str(payload.get("mime_type") or "application/octet-stream")
    try:
        size_bytes = int(payload.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    workspace_payload = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    payload["workspace"] = dict(workspace_payload)
    payload["relativePath"] = payload.get("relative_path") or ""
    payload["mime"] = mime_type
    payload["mimeType"] = mime_type
    payload["kind"] = _artifact_kind(mime_type)
    payload["size"] = size_bytes
    payload["size_bytes"] = size_bytes
    payload["sizeBytes"] = size_bytes
    payload["workspacePayload"] = payload["workspace"]
    payload["workspace_payload"] = payload["workspace"]
    origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
    produced_by_run_id = str(
        origin.get("run_id")
        or origin.get("runId")
        or origin.get("produced_by_run_id")
        or origin.get("producedByRunId")
        or ""
    )
    payload["produced_by_run_id"] = produced_by_run_id
    payload["producedByRunId"] = produced_by_run_id
    return payload


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
    workspace_snapshot: WorkspaceArtifactSnapshot | None = None,
) -> list[ArtifactRecord]:
    workspace_payload = _workspace_payload(workspace, cwd)
    workspace_path = normalize_session_cwd(workspace_payload["path"])
    records: list[ArtifactRecord] = []

    terminal_changes = changed_workspace_artifacts(workspace_snapshot)
    terminal_operations = {change.path: change.operation for change in terminal_changes}
    target_changes = [
        ArtifactTarget(path=change.path, operation=change.operation)
        for change in terminal_changes
    ] or artifact_target_changes(name, args, result)

    for target in target_changes:
        artifact_path = resolve_artifact_path(target.path, cwd)
        if not is_path_inside(artifact_path, workspace_path):
            continue
        mime_type = mimetypes.guess_type(artifact_path)[0] or "application/octet-stream"
        artifact_origin = {
            "event": "tool.complete",
            "tool_id": tool_call_id,
            "tool_name": name,
            **dict(origin or {}),
        }
        operation = str(target.operation or "")
        is_workspace_diff = artifact_path in terminal_operations
        if operation and (operation == "deleted" or is_workspace_diff):
            artifact_origin["operation"] = operation
        if is_workspace_diff:
            artifact_origin["source"] = "workspace_diff"
        if operation == "deleted":
            records.append(
                ArtifactRecord(
                    id=_artifact_id(workspace_payload["id"], artifact_path),
                    workspace_id=workspace_payload["id"],
                    path=artifact_path,
                    relative_path=_relative_artifact_path(artifact_path, workspace_path),
                    title=os.path.basename(artifact_path),
                    mime_type=mime_type,
                    size_bytes=0,
                    workspace=workspace_payload,
                    origin=artifact_origin,
                    operation="deleted",
                )
            )
            continue
        if not os.path.isfile(artifact_path):
            continue
        stat = os.stat(artifact_path)
        operation = terminal_operations.get(artifact_path)
        if operation:
            artifact_origin["operation"] = operation
            artifact_origin["source"] = "workspace_diff"
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


def _artifact_payload_from_record(record: ArtifactRecord) -> dict[str, Any]:
    payload = record.to_payload()
    if record.operation == "deleted":
        payload = _artifact_payload_contract(payload)
        payload["availability"] = "missing"
        now = time.time()
        payload.setdefault("created_at", now)
        payload.setdefault("updated_at", now)
    return payload


def artifact_payloads_from_tool_complete(
    *,
    tool_call_id: str,
    name: str,
    args: dict,
    result: str,
    cwd: str,
    workspace: dict[str, Any] | None,
    origin: dict[str, Any] | None = None,
    workspace_snapshot: WorkspaceArtifactSnapshot | None = None,
) -> list[dict]:
    return [
        _artifact_payload_from_record(record)
        for record in _artifact_records_from_tool_complete(
            tool_call_id=tool_call_id,
            name=name,
            args=args,
            result=result,
            cwd=normalize_session_cwd(cwd),
            workspace=workspace,
            origin=origin,
            workspace_snapshot=workspace_snapshot,
        )
    ]


def artifact_created_payloads_from_tool_complete(
    *,
    tool_call_id: str,
    name: str,
    args: dict,
    result: str,
    cwd: str,
    workspace: dict[str, Any] | None,
    origin: dict[str, Any] | None = None,
    workspace_snapshot: WorkspaceArtifactSnapshot | None = None,
) -> list[dict]:
    return [
        _artifact_payload_from_record(record)
        for record in _artifact_records_from_tool_complete(
            tool_call_id=tool_call_id,
            name=name,
            args=args,
            result=result,
            cwd=normalize_session_cwd(cwd),
            workspace=workspace,
            origin=origin,
            workspace_snapshot=workspace_snapshot,
        )
        if record.operation != "deleted"
    ]


def _deleted_artifact_payload(
    *,
    store,
    session_id: str,
    record: ArtifactRecord,
) -> dict[str, Any]:
    base_payload = _artifact_payload_from_record(record)
    deleted = store.delete_artifact(
        session_id=session_id,
        artifact_id=record.id,
        path=record.path,
        workspace_id=record.workspace_id,
    )
    existing = deleted.get("artifact") if isinstance(deleted, dict) else None
    if isinstance(existing, dict) and existing:
        payload = dict(existing)
        payload["origin"] = dict(record.origin)
        payload["operation"] = "deleted"
        payload["availability"] = "missing"
        payload["updated_at"] = time.time()
        return _artifact_payload_contract(payload)
    return base_payload


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
    workspace_snapshot: WorkspaceArtifactSnapshot | None = None,
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
        workspace_snapshot=workspace_snapshot,
    ):
        if record.operation == "deleted":
            payloads.append(
                _deleted_artifact_payload(
                    store=store,
                    session_id=session_id,
                    record=record,
                )
            )
            continue
        persisted = store.upsert_artifact(
            artifact=record.to_payload(),
            session_id=session_id,
        )
        persisted["workspace"] = persisted_workspace
        payloads.append(persisted)
    return payloads


def register_artifact(
    *,
    session_id: str,
    path: str,
    cwd: str,
    workspace: dict[str, Any] | None,
    title: str = "",
    mime_type: str = "",
    artifact_id: str = "",
    origin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("session_id required")
    cwd = normalize_session_cwd(cwd)
    workspace_payload = _workspace_payload(workspace, cwd)
    workspace_path = normalize_session_cwd(workspace_payload["path"])
    artifact_path = resolve_artifact_path(path, cwd)
    if not is_path_inside(artifact_path, workspace_path):
        raise ValueError("artifact path must be inside workspace")
    if not os.path.isfile(artifact_path):
        raise ValueError("artifact path must be a file")
    stat = os.stat(artifact_path)
    persisted_workspace = bind_session_workspace(
        session_id=session_id,
        cwd=cwd,
        workspace=workspace_payload,
    )
    mime = str(mime_type or "").strip() or mimetypes.guess_type(artifact_path)[0] or "application/octet-stream"
    record = ArtifactRecord(
        id=str(artifact_id or "").strip() or _artifact_id(persisted_workspace["id"], artifact_path),
        workspace_id=persisted_workspace["id"],
        path=artifact_path,
        relative_path=_relative_artifact_path(artifact_path, workspace_path),
        title=str(title or "").strip() or os.path.basename(artifact_path),
        mime_type=mime,
        size_bytes=int(stat.st_size),
        workspace=persisted_workspace,
        origin={
            "event": "desktop.register",
            **dict(origin or {}),
        },
    )
    persisted = get_gateway_state_store().upsert_artifact(
        artifact=record.to_payload(),
        session_id=session_id,
    )
    persisted["workspace"] = persisted_workspace
    return persisted


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


def delete_artifact(
    *,
    session_id: str,
    artifact_id: str = "",
    path: str = "",
    workspace_id: str = "",
) -> dict[str, Any]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return {"deleted": False, "artifact": None}
    return store.delete_artifact(
        session_id=session_id,
        artifact_id=artifact_id,
        path=path,
        workspace_id=workspace_id,
    )


def delete_session_artifacts(session_ids: list[str]) -> dict[str, Any]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return {
            "skipped": True,
            "deleted_artifact_links": 0,
            "deleted_artifacts": 0,
            "deleted_artifact_ids": [],
            "physical_files_deleted": 0,
        }
    return store.delete_session_artifacts(session_ids)


def prune_artifacts(
    *,
    session_id: str = "",
    workspace_id: str = "",
    retention_days: int = 30,
    max_artifacts_per_session: int = 500,
    max_artifacts_per_workspace: int = 2000,
) -> dict[str, Any]:
    store = get_gateway_state_store(create_if_missing=False)
    if store is None:
        return {
            "skipped": True,
            "deleted_artifact_links": 0,
            "deleted_artifacts": 0,
            "physical_files_deleted": 0,
        }
    return store.prune_artifacts(
        session_id=session_id,
        workspace_id=workspace_id,
        retention_days=retention_days,
        max_artifacts_per_session=max_artifacts_per_session,
        max_artifacts_per_workspace=max_artifacts_per_workspace,
    )
