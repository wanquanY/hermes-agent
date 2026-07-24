"""Extract artifact candidates from tool completion payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactTarget:
    path: str
    operation: str = ""
    title: str = ""
    mime_type: str = ""


def parse_tool_result_dict(result: str) -> dict:
    if not isinstance(result, str):
        return {}
    try:
        data = json.loads(result.strip())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def file_mutation_landed(name: str, data: dict) -> bool:
    if data.get("error"):
        return False
    if name == "write_file":
        return "bytes_written" in data
    if name == "patch":
        return data.get("success") is True
    return False


def patch_argument_targets(args: dict) -> list[str]:
    mode = str(args.get("mode") or "replace")
    if mode == "replace":
        return [str(args["path"])] if args.get("path") else []
    if mode != "patch":
        return []
    patch_body = args.get("patch") or ""
    if not isinstance(patch_body, str) or not patch_body:
        return []

    paths: list[str] = []
    for match in re.finditer(
        r"^\*\*\*\s+(?:Update|Add)\s+File:\s*(.+)$",
        patch_body,
        re.MULTILINE,
    ):
        file_path = match.group(1).strip()
        if file_path:
            paths.append(file_path)
    return paths


def patch_argument_deleted_targets(args: dict) -> list[str]:
    if str(args.get("mode") or "replace") != "patch":
        return []
    patch_body = args.get("patch") or ""
    if not isinstance(patch_body, str) or not patch_body:
        return []

    paths: list[str] = []
    for match in re.finditer(
        r"^\*\*\*\s+Delete\s+File:\s*(.+)$",
        patch_body,
        re.MULTILINE,
    ):
        file_path = match.group(1).strip()
        if file_path:
            paths.append(file_path)
    return paths


def _dedupe_targets(targets: list[ArtifactTarget]) -> list[ArtifactTarget]:
    deduped: list[ArtifactTarget] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        path = str(target.path or "")
        if not path:
            continue
        operation = str(target.operation or "")
        key = (path, operation)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def result_artifact_targets(data: dict) -> list[ArtifactTarget]:
    """Extract explicit artifact records returned by a tool.

    This result contract lets domain tools register only their durable,
    user-facing outputs without taking an expensive whole-workspace snapshot.
    The registry service still verifies that every path exists inside the
    active workspace before persisting it.
    """

    values = data.get("artifacts")
    if not isinstance(values, list):
        return []
    targets: list[ArtifactTarget] = []
    for value in values:
        if isinstance(value, str):
            path = value.strip()
            if path:
                targets.append(ArtifactTarget(path=path))
            continue
        if not isinstance(value, dict):
            continue
        path = str(
            value.get("path")
            or value.get("artifact_path")
            or value.get("artifactPath")
            or ""
        ).strip()
        if not path:
            continue
        operation = str(value.get("operation") or "").strip().lower()
        if operation not in {"created", "deleted", "modified"}:
            operation = ""
        targets.append(
            ArtifactTarget(
                path=path,
                operation=operation,
                title=str(value.get("title") or "").strip(),
                mime_type=str(
                    value.get("mime_type")
                    or value.get("mimeType")
                    or value.get("mime")
                    or ""
                ).strip(),
            )
        )
    return _dedupe_targets(targets)


def artifact_target_changes(name: str, args: dict, result: str) -> list[ArtifactTarget]:
    data = parse_tool_result_dict(result)
    explicit_targets = result_artifact_targets(data)
    if explicit_targets:
        return explicit_targets
    if name not in {"write_file", "patch"}:
        return []
    if not file_mutation_landed(name, data):
        return []

    targets: list[ArtifactTarget] = []
    if name == "write_file":
        if args.get("path"):
            targets.append(ArtifactTarget(path=str(args["path"])))
    else:
        for key, operation in (
            ("files_created", "created"),
            ("files_modified", "modified"),
            ("files_deleted", "deleted"),
        ):
            value = data.get(key)
            if isinstance(value, list):
                targets.extend(
                    ArtifactTarget(path=str(item), operation=operation)
                    for item in value
                    if item
                )
        if not targets:
            targets.extend(ArtifactTarget(path=item) for item in patch_argument_targets(args))
            targets.extend(
                ArtifactTarget(path=item, operation="deleted")
                for item in patch_argument_deleted_targets(args)
            )

    return _dedupe_targets(targets)


def artifact_target_paths(name: str, args: dict, result: str) -> list[str]:
    paths = [
        target.path
        for target in artifact_target_changes(name, args, result)
        if target.operation != "deleted"
    ]

    deduped: list[str] = []
    seen: set[str] = set()
    for item in paths:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped
