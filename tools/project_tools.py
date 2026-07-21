#!/usr/bin/env python3
"""Intentional agent controls for first-class desktop projects.

The tool layer depends on the canonical session store composition root; it
does not own a second projects database. GUI gateways may install a workspace
callback so successful create/switch operations re-anchor the live session.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from tools.registry import registry

_workspace_callback: Optional[Callable[[str, str, str], None]] = None


def set_project_workspace_callback(
    callback: Optional[Callable[[str, str, str], None]],
) -> None:
    global _workspace_callback
    _workspace_callback = callback


def _primary_path(project) -> str | None:
    if project.primary_path:
        return project.primary_path
    for folder in project.folders:
        if folder.is_primary:
            return folder.path
    return project.folders[0].path if project.folders else None


def _apply_workspace(task_id: str | None, path: str | None, name: str) -> None:
    if _workspace_callback is None or not task_id or not path:
        return
    try:
        _workspace_callback(task_id, path, name)
    except Exception:
        # The durable project mutation succeeded. A disconnected GUI must not
        # convert that success into a misleading tool failure.
        pass


def _resolve(service, token: str):
    value = str(token or "").strip()
    if not value:
        return None
    project = service.get(value)
    if project is not None:
        return project
    folded = value.casefold()
    return next(
        (
            item
            for item in service.list(include_archived=True)
            if item.name.casefold() == folded or item.slug.casefold() == folded
        ),
        None,
    )


def project_list(task_id: str | None = None) -> str:
    del task_id
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    store = open_cli_session_store()
    try:
        active_id = store.projects.active_id()
        projects = store.projects.list()
    finally:
        store.close()
    return json.dumps(
        {
            "active_id": active_id,
            "projects": [
                {
                    "id": project.id,
                    "slug": project.slug,
                    "name": project.name,
                    "primary_path": _primary_path(project),
                    "active": project.id == active_id,
                }
                for project in projects
            ],
        },
        ensure_ascii=False,
    )


def project_create(
    name: str,
    path: str | None = None,
    task_id: str | None = None,
) -> str:
    project_name = str(name or "").strip()
    if not project_name:
        return json.dumps({"success": False, "error": "name is required"})
    folder = str(path or "").strip()
    if folder:
        folder = os.path.abspath(os.path.expanduser(folder))

    from hermes_agent.composition.cli_session_store import open_cli_session_store

    store = open_cli_session_store()
    try:
        project = store.projects.create(
            name=project_name,
            folders=[folder] if folder else [],
            primary_path=folder or None,
        )
        store.projects.set_active(project.id)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    finally:
        store.close()

    primary = _primary_path(project)
    _apply_workspace(task_id, primary, project.name)
    return json.dumps(
        {
            "success": True,
            "id": project.id,
            "slug": project.slug,
            "name": project.name,
            "primary_path": primary,
        },
        ensure_ascii=False,
    )


def project_switch(project: str, task_id: str | None = None) -> str:
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    store = open_cli_session_store()
    try:
        selected = _resolve(store.projects, project)
        if selected is None:
            return json.dumps(
                {"success": False, "error": f"no project matching '{project}'"},
                ensure_ascii=False,
            )
        store.projects.set_active(selected.id)
    finally:
        store.close()

    primary = _primary_path(selected)
    _apply_workspace(task_id, primary, selected.name)
    return json.dumps(
        {
            "success": True,
            "id": selected.id,
            "slug": selected.slug,
            "name": selected.name,
            "primary_path": primary,
        },
        ensure_ascii=False,
    )


registry.register(
    name="project_list",
    toolset="project",
    schema={
        "name": "project_list",
        "description": "List desktop Projects and identify the active workspace.",
        "parameters": {"type": "object", "properties": {}},
    },
    handler=lambda args, **kwargs: project_list(task_id=kwargs.get("task_id")),
)

registry.register(
    name="project_create",
    toolset="project",
    schema={
        "name": "project_create",
        "description": (
            "Create and activate a named desktop Project. Pass path to anchor "
            "this chat to a repository or folder."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human project name"},
                "path": {"type": "string", "description": "Primary repository or folder"},
            },
            "required": ["name"],
        },
    },
    handler=lambda args, **kwargs: project_create(
        name=args.get("name", ""),
        path=args.get("path"),
        task_id=kwargs.get("task_id"),
    ),
)

registry.register(
    name="project_switch",
    toolset="project",
    schema={
        "name": "project_switch",
        "description": (
            "Switch this desktop chat into an existing Project by name, slug, or id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name, slug, or id"},
            },
            "required": ["project"],
        },
    },
    handler=lambda args, **kwargs: project_switch(
        project=args.get("project", ""),
        task_id=kwargs.get("task_id"),
    ),
)
