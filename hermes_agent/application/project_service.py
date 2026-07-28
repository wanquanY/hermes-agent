"""Application service for first-class, multi-folder projects."""

from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
import secrets
import time
from typing import Any, Callable

from hermes_agent.domain.project import (
    Project,
    ProjectFolder,
    normalize_slug,
    slugify,
)
from hermes_agent.repositories.project_repo import ProjectRepository
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


class ProjectService:
    def __init__(
        self,
        repository: ProjectRepository,
        unit_of_work: SqliteUnitOfWork,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._clock = clock

    @staticmethod
    def _path(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("project folder path must not be empty")
        return str(Path(raw).expanduser().resolve(strict=False))

    def get(self, identity: str) -> Project | None:
        value = str(identity or "").strip()
        if not value:
            return None
        return self._unit_of_work.read(lambda _conn: self._repository.get(value))

    def list(self, *, include_archived: bool = False) -> list[Project]:
        return self._unit_of_work.read(
            lambda _conn: self._repository.list(include_archived=include_archived)
        )

    def _unique_slug(self, candidate: str) -> str:
        base = candidate
        suffix = 1
        value = base
        while self._repository.slug_exists(value):
            suffix += 1
            marker = f"-{suffix}"
            value = f"{base[: 64 - len(marker)].rstrip('-_')}{marker}"
        return value

    def create(
        self,
        *,
        name: str,
        slug: str | None = None,
        folders: Iterable[str] = (),
        primary_path: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        board_slug: str | None = None,
    ) -> Project:
        project_name = str(name or "").strip()
        if not project_name:
            raise ValueError("project name must not be empty")
        normalized_folders = list(
            dict.fromkeys(self._path(folder) for folder in folders if str(folder).strip())
        )
        primary = self._path(primary_path) if primary_path else None
        if primary and primary not in normalized_folders:
            normalized_folders.insert(0, primary)
        if primary is None and normalized_folders:
            primary = normalized_folders[0]
        created_at = float(self._clock())

        def write(_conn) -> Project:
            project = Project(
                id=f"p_{secrets.token_hex(4)}",
                slug=self._unique_slug(normalize_slug(slug) or slugify(project_name)),
                name=project_name,
                description=description,
                icon=icon,
                color=color,
                board_slug=normalize_slug(board_slug) if board_slug else None,
                primary_path=primary,
                created_at=created_at,
            )
            folder_models = tuple(
                ProjectFolder(
                    path=path,
                    is_primary=path == primary,
                    added_at=created_at,
                )
                for path in normalized_folders
            )
            self._repository.insert(project, folder_models)
            stored = self._repository.get(project.id)
            if stored is None:  # pragma: no cover - insert invariant
                raise RuntimeError("project did not materialize after insert")
            return stored

        return self._unit_of_work.execute(write)

    def update(
        self,
        identity: str,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        board_slug: str | None = None,
    ) -> Project:
        project = self.get(identity)
        if project is None:
            raise ValueError(f"no such project: {identity}")
        fields: dict[str, Any] = {}
        if name is not None:
            value = str(name).strip()
            if not value:
                raise ValueError("project name must not be empty")
            fields["name"] = value
        if description is not None:
            fields["description"] = description or None
        if icon is not None:
            fields["icon"] = icon or None
        if color is not None:
            fields["color"] = color or None
        if board_slug is not None:
            fields["board_slug"] = normalize_slug(board_slug) if board_slug.strip() else None

        def write(_conn) -> Project:
            self._repository.update_fields(project.id, fields)
            updated = self._repository.get(project.id)
            if updated is None:  # pragma: no cover - update invariant
                raise RuntimeError("project vanished during update")
            return updated

        return self._unit_of_work.execute(write)

    def add_folder(
        self,
        identity: str,
        path: str,
        *,
        label: str | None = None,
        is_primary: bool = False,
    ) -> Project:
        project = self.get(identity)
        if project is None:
            raise ValueError(f"no such project: {identity}")
        normalized = self._path(path)

        def write(_conn) -> Project:
            self._repository.add_folder(
                project.id,
                ProjectFolder(
                    path=normalized,
                    label=label,
                    added_at=float(self._clock()),
                ),
            )
            current = self._repository.get(project.id)
            if is_primary or not (current and current.primary_path):
                self._repository.set_primary(project.id, normalized)
            updated = self._repository.get(project.id)
            if updated is None:  # pragma: no cover
                raise RuntimeError("project vanished during folder add")
            return updated

        return self._unit_of_work.execute(write)

    def remove_folder(self, identity: str, path: str) -> Project:
        project = self.get(identity)
        if project is None:
            raise ValueError(f"no such project: {identity}")
        normalized = self._path(path)

        def write(_conn) -> Project:
            removed, was_primary = self._repository.remove_folder(project.id, normalized)
            if not removed:
                raise ValueError(f"folder not in project: {path}")
            if was_primary:
                self._repository.set_primary(
                    project.id,
                    self._repository.first_folder_path(project.id),
                )
            updated = self._repository.get(project.id)
            if updated is None:  # pragma: no cover
                raise RuntimeError("project vanished during folder removal")
            return updated

        return self._unit_of_work.execute(write)

    def set_primary(self, identity: str, path: str) -> Project:
        project = self.get(identity)
        if project is None:
            raise ValueError(f"no such project: {identity}")
        normalized = self._path(path)

        def write(_conn) -> Project:
            if not self._repository.folder_exists(project.id, normalized):
                raise ValueError(f"folder is not part of project: {path}")
            self._repository.set_primary(project.id, normalized)
            updated = self._repository.get(project.id)
            if updated is None:  # pragma: no cover
                raise RuntimeError("project vanished during primary update")
            return updated

        return self._unit_of_work.execute(write)

    def set_archived(self, identity: str, archived: bool) -> Project:
        project = self.get(identity)
        if project is None:
            raise ValueError(f"no such project: {identity}")

        def write(_conn) -> Project:
            self._repository.update_fields(project.id, {"archived": int(archived)})
            updated = self._repository.get(project.id)
            if updated is None:  # pragma: no cover
                raise RuntimeError("project vanished during archive update")
            return updated

        return self._unit_of_work.execute(write)

    def delete(self, identity: str) -> bool:
        project = self.get(identity)
        if project is None:
            return False

        def write(_conn) -> bool:
            if self._repository.active_id() == project.id:
                self._repository.set_active(None)
            return self._repository.delete(project.id)

        return self._unit_of_work.execute(write)

    def set_active(self, identity: str | None) -> str | None:
        project = self.get(identity) if identity else None
        if identity and project is None:
            raise ValueError(f"no such project: {identity}")
        project_id = project.id if project else None
        self._unit_of_work.execute(
            lambda _conn: self._repository.set_active(project_id)
        )
        return project_id

    def active_id(self) -> str | None:
        return self._unit_of_work.read(lambda _conn: self._repository.active_id())

    def project_for_path(
        self,
        path: str,
        *,
        include_archived: bool = False,
    ) -> Project | None:
        if not str(path or "").strip():
            return None
        target = self._path(path)

        def read(_conn) -> Project | None:
            best_id = ""
            best_length = -1
            for project_id, folder in self._repository.folder_owners(
                include_archived=include_archived
            ):
                try:
                    Path(target).relative_to(folder)
                except ValueError:
                    continue
                if len(folder) > best_length:
                    best_id = project_id
                    best_length = len(folder)
            return self._repository.get(best_id) if best_id else None

        return self._unit_of_work.read(read)

    def record_discovered_repos(
        self,
        repos: Iterable[tuple[str, str | None]],
        *,
        replace: bool = False,
    ) -> int:
        now = float(self._clock())
        rows = []
        for root, label in repos:
            normalized = self._path(root)
            rows.append((normalized, label or os.path.basename(normalized) or normalized, now))
        return self._unit_of_work.execute(
            lambda _conn: self._repository.record_discovered(rows, replace=replace)
        )

    def list_discovered_repos(self) -> list[dict[str, Any]]:
        return self._unit_of_work.read(
            lambda _conn: self._repository.list_discovered()
        )


__all__ = ["ProjectService"]
