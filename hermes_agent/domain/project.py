"""First-class project domain model."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BRANCH_SAFE_RE = re.compile(r"[^a-z0-9._-]+")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    return slug.strip("-_")[:64].strip("-_") or "project"


def normalize_slug(value: str | None) -> str | None:
    if value is None:
        return None
    slug = str(value).strip().lower()
    if not slug:
        return None
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            "project slug must be 1-64 lowercase alphanumeric, hyphen, or "
            "underscore characters and start with an alphanumeric character"
        )
    return slug


@dataclass(frozen=True)
class ProjectFolder:
    path: str
    label: str | None = None
    is_primary: bool = False
    added_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "label": self.label,
            "is_primary": self.is_primary,
            "added_at": self.added_at,
        }


@dataclass(frozen=True)
class Project:
    id: str
    slug: str
    name: str
    created_at: float
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    board_slug: str | None = None
    primary_path: str | None = None
    archived: bool = False
    folders: tuple[ProjectFolder, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "board_slug": self.board_slug,
            "primary_path": self.primary_path,
            "archived": self.archived,
            "created_at": self.created_at,
            "folders": [folder.to_dict() for folder in self.folders],
        }

    def branch_name(self, task_id: str, *, title: str = "") -> str:
        base = f"{self.slug or slugify(self.name)}/{str(task_id).strip()}"
        title_slug = _BRANCH_SAFE_RE.sub("-", str(title).strip().lower()).strip("-")
        title_slug = title_slug[:40].strip("-")
        return f"{base}-{title_slug}" if title_slug else base


__all__ = ["Project", "ProjectFolder", "normalize_slug", "slugify"]
