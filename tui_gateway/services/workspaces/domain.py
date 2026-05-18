"""Workspace domain objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    path: str
    kind: str = ""
    created_at: float | None = None
    updated_at: float | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at
        if self.updated_at is not None:
            payload["updated_at"] = self.updated_at
        return payload


@dataclass(frozen=True)
class SessionWorkspaceBinding:
    session_id: str
    workspace_id: str
    cwd: str
    workspace: Workspace

    def to_payload(self) -> dict[str, Any]:
        payload = self.workspace.to_payload()
        payload["session_id"] = self.session_id
        payload["cwd"] = self.cwd
        return payload

