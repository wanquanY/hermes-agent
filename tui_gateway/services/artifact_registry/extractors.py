"""Extract artifact candidates from tool completion payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactTarget:
    path: str
    operation: str = ""


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
        deduped.append(ArtifactTarget(path=path, operation=operation))
    return deduped


def artifact_target_changes(name: str, args: dict, result: str) -> list[ArtifactTarget]:
    if name not in {"write_file", "patch"}:
        return []
    data = parse_tool_result_dict(result)
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
