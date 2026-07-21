"""Persistence owner for projects and their folder membership."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hermes_agent.domain.project import Project, ProjectFolder
from hermes_agent.repositories.base import RepositoryConnection


class ProjectRepository:
    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def _folders(self, project_id: str) -> tuple[ProjectFolder, ...]:
        rows = self._conn.execute(
            "SELECT path, label, is_primary, added_at FROM project_folders "
            "WHERE project_id = ? ORDER BY is_primary DESC, added_at ASC",
            (project_id,),
        ).fetchall()
        return tuple(
            ProjectFolder(
                path=str(row["path"]),
                label=(str(row["label"]) if row["label"] is not None else None),
                is_primary=bool(row["is_primary"]),
                added_at=float(row["added_at"] or 0),
            )
            for row in rows
        )

    def _project(self, row: Any | None) -> Project | None:
        if row is None:
            return None
        project_id = str(row["id"])
        return Project(
            id=project_id,
            slug=str(row["slug"]),
            name=str(row["name"]),
            created_at=float(row["created_at"] or 0),
            description=(
                str(row["description"]) if row["description"] is not None else None
            ),
            icon=str(row["icon"]) if row["icon"] is not None else None,
            color=str(row["color"]) if row["color"] is not None else None,
            board_slug=(
                str(row["board_slug"]) if row["board_slug"] is not None else None
            ),
            primary_path=(
                str(row["primary_path"])
                if row["primary_path"] is not None
                else None
            ),
            archived=bool(row["archived"]),
            folders=self._folders(project_id),
        )

    def get(self, identity: str) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ? OR slug = ? "
            "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
            (identity, identity.lower(), identity),
        ).fetchone()
        return self._project(row)

    def list(self, *, include_archived: bool = False) -> list[Project]:
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at ASC, id ASC"
        return [
            project
            for row in self._conn.execute(sql).fetchall()
            if (project := self._project(row)) is not None
        ]

    def slug_exists(self, slug: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM projects WHERE slug = ?",
            (slug,),
        ).fetchone() is not None

    def insert(
        self,
        project: Project,
        folders: Iterable[ProjectFolder],
    ) -> None:
        self._conn.execute(
            "INSERT INTO projects (id, slug, name, description, icon, color, "
            "board_slug, primary_path, created_at, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project.id,
                project.slug,
                project.name,
                project.description,
                project.icon,
                project.color,
                project.board_slug,
                project.primary_path,
                project.created_at,
                int(project.archived),
            ),
        )
        self._conn.executemany(
            "INSERT INTO project_folders "
            "(project_id, path, label, is_primary, added_at) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    project.id,
                    folder.path,
                    folder.label,
                    int(folder.is_primary),
                    folder.added_at,
                )
                for folder in folders
            ],
        )

    def update_fields(self, project_id: str, fields: dict[str, Any]) -> bool:
        if not fields:
            return False
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cursor = self._conn.execute(
            f"UPDATE projects SET {assignments} WHERE id = ?",
            (*fields.values(), project_id),
        )
        return cursor.rowcount > 0

    def add_folder(self, project_id: str, folder: ProjectFolder) -> None:
        self._conn.execute(
            "INSERT INTO project_folders "
            "(project_id, path, label, is_primary, added_at) VALUES (?, ?, ?, 0, ?) "
            "ON CONFLICT(project_id, path) DO UPDATE SET label = excluded.label",
            (project_id, folder.path, folder.label, folder.added_at),
        )

    def remove_folder(self, project_id: str, path: str) -> tuple[bool, bool]:
        row = self._conn.execute(
            "SELECT is_primary FROM project_folders WHERE project_id = ? AND path = ?",
            (project_id, path),
        ).fetchone()
        cursor = self._conn.execute(
            "DELETE FROM project_folders WHERE project_id = ? AND path = ?",
            (project_id, path),
        )
        return cursor.rowcount > 0, bool(row and row["is_primary"])

    def first_folder_path(self, project_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT path FROM project_folders WHERE project_id = ? "
            "ORDER BY added_at ASC, path ASC LIMIT 1",
            (project_id,),
        ).fetchone()
        return str(row["path"]) if row else None

    def folder_exists(self, project_id: str, path: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM project_folders WHERE project_id = ? AND path = ?",
            (project_id, path),
        ).fetchone() is not None

    def set_primary(self, project_id: str, path: str | None) -> None:
        self._conn.execute(
            "UPDATE project_folders SET is_primary = 0 WHERE project_id = ?",
            (project_id,),
        )
        if path is not None:
            self._conn.execute(
                "UPDATE project_folders SET is_primary = 1 "
                "WHERE project_id = ? AND path = ?",
                (project_id, path),
            )
        self._conn.execute(
            "UPDATE projects SET primary_path = ? WHERE id = ?",
            (path, project_id),
        )

    def delete(self, project_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0

    def set_active(self, project_id: str | None) -> None:
        if project_id is None:
            self._conn.execute("DELETE FROM project_meta WHERE key = 'active_id'")
            return
        self._conn.execute(
            "INSERT INTO project_meta (key, value) VALUES ('active_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (project_id,),
        )

    def active_id(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM project_meta WHERE key = 'active_id'"
        ).fetchone()
        return str(row["value"]) if row else None

    def folder_owners(self, *, include_archived: bool = False) -> list[tuple[str, str]]:
        sql = (
            "SELECT pf.project_id, pf.path FROM project_folders pf "
            "JOIN projects p ON p.id = pf.project_id"
        )
        if not include_archived:
            sql += " WHERE p.archived = 0"
        return [
            (str(row["project_id"]), str(row["path"]))
            for row in self._conn.execute(sql).fetchall()
        ]

    def record_discovered(
        self,
        rows: Iterable[tuple[str, str, float]],
        *,
        replace: bool,
    ) -> int:
        values = list(rows)
        if replace:
            self._conn.execute("DELETE FROM discovered_repos")
        self._conn.executemany(
            "INSERT INTO discovered_repos (root, label, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(root) DO UPDATE SET label = excluded.label, "
            "last_seen = excluded.last_seen",
            values,
        )
        return len(values)

    def list_discovered(self) -> list[dict[str, Any]]:
        return [
            {
                "root": str(row["root"]),
                "label": str(row["label"] or ""),
                "last_seen": float(row["last_seen"] or 0),
            }
            for row in self._conn.execute(
                "SELECT root, label, last_seen FROM discovered_repos "
                "ORDER BY last_seen DESC, root ASC"
            ).fetchall()
        ]


__all__ = ["ProjectRepository"]
