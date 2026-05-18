"""Artifact domain objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    workspace_id: str
    path: str
    relative_path: str
    title: str
    mime_type: str
    size_bytes: int
    workspace: dict[str, Any]
    origin: dict[str, Any]
    created_at: float | None = None
    updated_at: float | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "path": self.path,
            "relative_path": self.relative_path,
            "title": self.title,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "workspace": dict(self.workspace),
            "origin": dict(self.origin),
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at
        if self.updated_at is not None:
            payload["updated_at"] = self.updated_at
        return payload

