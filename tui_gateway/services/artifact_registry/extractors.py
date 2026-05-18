"""Extract artifact candidates from tool completion payloads."""

from __future__ import annotations

import json
import re


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


def artifact_target_paths(name: str, args: dict, result: str) -> list[str]:
    if name not in {"write_file", "patch"}:
        return []
    data = parse_tool_result_dict(result)
    if not file_mutation_landed(name, data):
        return []

    paths: list[str] = []
    if name == "write_file":
        if args.get("path"):
            paths.append(str(args["path"]))
    else:
        for key in ("files_created", "files_modified"):
            value = data.get(key)
            if isinstance(value, list):
                paths.extend(str(item) for item in value if item)
        if not paths:
            paths.extend(patch_argument_targets(args))

    deduped: list[str] = []
    seen: set[str] = set()
    for item in paths:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped

