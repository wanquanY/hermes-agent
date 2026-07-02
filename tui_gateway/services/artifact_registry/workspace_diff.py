"""Workspace file snapshots for terminal-produced artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from tui_gateway.services.workspaces import normalize_session_cwd, workspace_from_params


IGNORED_DIR_NAMES = frozenset({
    ".dovie",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
})
IGNORED_FILE_NAMES = frozenset({
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
})
DEFAULT_MAX_SNAPSHOT_FILES = 5_000


@dataclass(frozen=True)
class WorkspaceFileState:
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class WorkspaceArtifactSnapshot:
    workspace_path: str
    files: dict[str, WorkspaceFileState]
    truncated: bool = False


@dataclass(frozen=True)
class WorkspaceArtifactChange:
    path: str
    operation: str


def _normalize_tool_name(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_")


def supports_workspace_artifact_snapshot(name: str) -> bool:
    return _normalize_tool_name(name) == "terminal"


def _workspace_path(cwd: str, workspace: dict[str, Any] | None) -> str:
    workspace_payload = workspace_from_params({"workspace": dict(workspace or {})}, cwd)
    return normalize_session_cwd(workspace_payload["path"])


def _should_skip_dir(name: str) -> bool:
    return name in IGNORED_DIR_NAMES


def _should_skip_file(name: str) -> bool:
    return name in IGNORED_FILE_NAMES


def snapshot_workspace_files(
    workspace_path: str,
    *,
    max_files: int = DEFAULT_MAX_SNAPSHOT_FILES,
) -> WorkspaceArtifactSnapshot:
    root = normalize_session_cwd(workspace_path)
    files: dict[str, WorkspaceFileState] = {}
    if not root or not os.path.isdir(root):
        return WorkspaceArtifactSnapshot(workspace_path=root, files=files)

    queue = [root]
    truncated = False
    while queue:
        directory = queue.pop(0)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if not _should_skip_dir(entry.name):
                            queue.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False) or _should_skip_file(entry.name):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    files[os.path.abspath(entry.path)] = WorkspaceFileState(
                        path=os.path.abspath(entry.path),
                        size=int(stat.st_size),
                        mtime_ns=int(stat.st_mtime_ns),
                    )
                    if len(files) >= max_files:
                        truncated = True
                        queue.clear()
                        break
        except OSError:
            continue
    return WorkspaceArtifactSnapshot(workspace_path=root, files=files, truncated=truncated)


def capture_workspace_artifact_snapshot(
    *,
    name: str,
    cwd: str,
    workspace: dict[str, Any] | None,
) -> WorkspaceArtifactSnapshot | None:
    if not supports_workspace_artifact_snapshot(name):
        return None
    return snapshot_workspace_files(_workspace_path(cwd, workspace))


def changed_workspace_artifacts(
    before: WorkspaceArtifactSnapshot | None,
) -> list[WorkspaceArtifactChange]:
    if before is None or before.truncated:
        return []
    after = snapshot_workspace_files(before.workspace_path)
    if after.truncated:
        return []

    changes: list[WorkspaceArtifactChange] = []
    for path, current in after.files.items():
        previous = before.files.get(path)
        if previous is None:
            changes.append(WorkspaceArtifactChange(path=path, operation="created"))
            continue
        if previous.size != current.size or previous.mtime_ns != current.mtime_ns:
            changes.append(WorkspaceArtifactChange(path=path, operation="modified"))
    for path in before.files:
        if path not in after.files:
            changes.append(WorkspaceArtifactChange(path=path, operation="deleted"))
    return sorted(changes, key=lambda item: item.path)
