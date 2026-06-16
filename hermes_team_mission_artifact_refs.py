from __future__ import annotations

import json
import os
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_list(value: Any) -> list[Any]:
    loaded = None
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            loaded = None
    if loaded is None:
        loaded = value
    if isinstance(loaded, list):
        return loaded
    if isinstance(loaded, tuple):
        return list(loaded)
    if loaded in (None, ""):
        return []
    return [loaded]


def _basename(value: str) -> str:
    value = _text(value)
    if not value:
        return ""
    return os.path.basename(value.rstrip("/")) or value


def _artifact_ref_key(artifact: dict[str, Any] | None) -> str:
    if not isinstance(artifact, dict):
        return ""
    for key in (
        "path",
        "uri",
        "url",
        "id",
        "artifact_id",
        "artifactId",
        "relative_path",
        "relativePath",
    ):
        value = _text(artifact.get(key))
        if value:
            return f"{key}:{value}"
    return _json_dumps(artifact)


def dedupe_artifact_refs(artifacts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in artifacts or []:
        if not isinstance(artifact, dict):
            continue
        key = _artifact_ref_key(artifact)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(dict(artifact))
    return deduped


def normalize_artifact_ref(
    raw: Any,
    *,
    default_kind: str = "artifact",
    force: bool = False,
) -> dict[str, Any]:
    if isinstance(raw, str):
        value = _text(raw)
        if not value:
            return {}
        lowered = value.lower()
        is_url = lowered.startswith(("http://", "https://", "file://"))
        file_like_default = (_text(default_kind) or "").lower() in {"file", "file_read", "input_attachment"}
        key = "uri" if is_url else "path" if file_like_default or value.startswith(("/", "~", ".")) else "uri"
        return {
            key: value,
            "title": _basename(value),
            "kind": _text(default_kind) or "artifact",
        }
    if not isinstance(raw, dict):
        return {}

    path = _text(raw.get("path") or raw.get("localPath") or raw.get("local_path"))
    uri = _text(
        raw.get("uri")
        or raw.get("url")
        or raw.get("fileUrl")
        or raw.get("file_url")
        or raw.get("remoteUrl")
        or raw.get("remote_url")
        or raw.get("previewUrl")
        or raw.get("preview_url")
    )
    artifact_id = _text(raw.get("artifact_id") or raw.get("artifactId") or raw.get("fileId") or raw.get("file_id"))
    raw_id = _text(raw.get("id"))
    relative_path = _text(raw.get("relative_path") or raw.get("relativePath"))
    workspace_id = _text(raw.get("workspace_id") or raw.get("workspaceId"))

    has_location = bool(path or uri or artifact_id or relative_path)
    if not has_location and raw_id and force:
        has_location = True
    if not has_location:
        return {}

    title = _text(
        raw.get("title")
        or raw.get("name")
        or raw.get("fileName")
        or raw.get("file_name")
        or raw.get("display_name")
        or raw.get("displayName")
        or _basename(path or relative_path or uri or artifact_id or raw_id)
    )
    kind = _text(raw.get("kind") or raw.get("type") or default_kind) or "artifact"
    result: dict[str, Any] = {
        "title": title,
        "kind": kind,
    }
    if raw_id:
        result["id"] = raw_id
    if artifact_id:
        result["artifact_id"] = artifact_id
        result["artifactId"] = artifact_id
    if path:
        result["path"] = path
    if uri:
        result["uri"] = uri
    if relative_path:
        result["relative_path"] = relative_path
        result["relativePath"] = relative_path
    if workspace_id:
        result["workspace_id"] = workspace_id
        result["workspaceId"] = workspace_id
    mime_type = _text(raw.get("mime_type") or raw.get("mimeType") or raw.get("content_type") or raw.get("contentType"))
    if mime_type:
        result["mime_type"] = mime_type
        result["mimeType"] = mime_type
    size = raw.get("size_bytes") or raw.get("sizeBytes") or raw.get("size")
    if size not in (None, ""):
        result["size_bytes"] = size
        result["sizeBytes"] = size
    origin = raw.get("origin")
    if isinstance(origin, dict) and origin:
        result["origin"] = dict(origin)
    workspace = raw.get("workspace")
    if isinstance(workspace, dict) and workspace:
        result["workspace"] = dict(workspace)
    return result


_PAYLOAD_ARTIFACT_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("artifact_refs", "artifact"),
    ("artifactRefs", "artifact"),
    ("artifacts", "artifact"),
    ("attachments", "input_attachment"),
    ("files", "file"),
    ("files_read", "file_read"),
    ("filesRead", "file_read"),
    ("files_written", "file"),
    ("filesWritten", "file"),
    ("workspace_artifacts", "file"),
    ("workspaceArtifacts", "file"),
)


def artifact_refs_from_payload(payload: dict[str, Any] | None, *, event_type: str = "") -> list[dict[str, Any]]:
    payload = payload if isinstance(payload, dict) else {}
    refs: list[dict[str, Any]] = []
    if _text(event_type) == "artifact.created":
        ref = normalize_artifact_ref(payload, default_kind="file", force=True)
        if ref:
            refs.append(ref)
    for key in ("artifact", "artifact_ref", "artifactRef"):
        if isinstance(payload.get(key), dict):
            ref = normalize_artifact_ref(payload.get(key), default_kind="artifact", force=True)
            if ref:
                refs.append(ref)
    for key, default_kind in _PAYLOAD_ARTIFACT_LIST_FIELDS:
        if key not in payload:
            continue
        for raw in _json_list(payload.get(key)):
            ref = normalize_artifact_ref(raw, default_kind=default_kind, force=True)
            if ref:
                refs.append(ref)
    return dedupe_artifact_refs(refs)


def artifact_refs_from_event(event: dict[str, Any] | None) -> list[dict[str, Any]]:
    event = event if isinstance(event, dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return artifact_refs_from_payload(payload, event_type=_text(event.get("type")))


def artifact_refs_from_message_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    metadata = metadata if isinstance(metadata, dict) else {}
    refs = artifact_refs_from_payload(metadata)
    team_ref = metadata.get("team_mission") or metadata.get("teamMission")
    if isinstance(team_ref, dict):
        refs.extend(artifact_refs_from_payload(team_ref))
    return dedupe_artifact_refs(refs)
