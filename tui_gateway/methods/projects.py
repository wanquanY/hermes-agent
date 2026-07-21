"""First-class project JSON-RPC surface backed by the canonical state store."""

from __future__ import annotations

import os
from typing import Any, Callable

from hermes_constants import get_hermes_home
from tui_gateway import git_probe, project_tree
from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())

_E_PROJECTS = 5061
_E_NO_PROJECT = 5062
_E_PROJECT_ARG = 5063
_PROJECT_TREE_EXCLUDED_SOURCES = ["cron"]
_PROJECT_CONTEXT_MUTATIONS = {
    "projects.create",
    "projects.update",
    "projects.add_folder",
    "projects.remove_folder",
    "projects.set_primary",
    "projects.archive",
    "projects.delete",
}


class _NoProject(LookupError):
    pass


def _store():
    db = _server._get_db()
    if db is None:
        raise RuntimeError("session store unavailable")
    return db


def _payload(db) -> dict[str, Any]:
    return {
        "projects": [
            project.to_dict()
            for project in db.projects.list(include_archived=True)
        ],
        "active_id": db.projects.active_id(),
    }


def _require(db, params: dict) -> Any:
    project = db.projects.get(str(params.get("id") or ""))
    if project is None:
        raise _NoProject
    return project


def _project_method(name: str) -> Callable:
    def decorate(fn: Callable) -> Callable:
        @method(name)
        def handler(rid, params: dict) -> dict:
            try:
                db = _store()
                result = fn(rid, params, db)
                if name in _PROJECT_CONTEXT_MUTATIONS:
                    _refresh_live_project_contexts(db)
                return result
            except _NoProject:
                return _err(rid, _E_NO_PROJECT, "no such project")
            except ValueError as exc:
                return _err(rid, _E_PROJECT_ARG, str(exc))
            except Exception as exc:
                return _err(rid, _E_PROJECTS, str(exc))

        return handler

    return decorate


@_project_method("projects.list")
def _list(rid, _params: dict, db) -> dict:
    return _ok(rid, _payload(db))


@_project_method("projects.get")
def _get(rid, params: dict, db) -> dict:
    return _ok(rid, {"project": _require(db, params).to_dict()})


@_project_method("projects.create")
def _create(rid, params: dict, db) -> dict:
    project = db.projects.create(
        name=str(params.get("name") or ""),
        slug=params.get("slug"),
        folders=params.get("folders") or [],
        primary_path=params.get("primary_path"),
        description=params.get("description"),
        icon=params.get("icon"),
        color=params.get("color"),
        board_slug=params.get("board_slug"),
    )
    if params.get("use"):
        db.projects.set_active(project.id)
    return _ok(rid, {"project": project.to_dict()})


@_project_method("projects.update")
def _update(rid, params: dict, db) -> dict:
    project = _require(db, params)
    updated = db.projects.update(
        project.id,
        name=params.get("name"),
        description=params.get("description"),
        icon=params.get("icon"),
        color=params.get("color"),
        board_slug=params.get("board_slug"),
    )
    return _ok(rid, {"project": updated.to_dict()})


@_project_method("projects.add_folder")
def _add_folder(rid, params: dict, db) -> dict:
    project = _require(db, params)
    updated = db.projects.add_folder(
        project.id,
        str(params.get("path") or ""),
        label=params.get("label"),
        is_primary=bool(params.get("is_primary")),
    )
    git_probe.invalidate()
    return _ok(rid, {"project": updated.to_dict()})


@_project_method("projects.remove_folder")
def _remove_folder(rid, params: dict, db) -> dict:
    project = _require(db, params)
    updated = db.projects.remove_folder(project.id, str(params.get("path") or ""))
    return _ok(rid, {"project": updated.to_dict()})


@_project_method("projects.set_primary")
def _set_primary(rid, params: dict, db) -> dict:
    project = _require(db, params)
    updated = db.projects.set_primary(project.id, str(params.get("path") or ""))
    return _ok(rid, {"project": updated.to_dict()})


@_project_method("projects.archive")
def _archive(rid, params: dict, db) -> dict:
    project = _require(db, params)
    db.projects.set_archived(project.id, not bool(params.get("restore")))
    return _ok(rid, _payload(db))


@_project_method("projects.delete")
def _delete(rid, params: dict, db) -> dict:
    project = _require(db, params)
    db.projects.delete(project.id)
    return _ok(rid, _payload(db))


@_project_method("projects.set_active")
def _set_active(rid, params: dict, db) -> dict:
    project_id = db.projects.set_active(str(params.get("id") or "") or None)
    return _ok(rid, {"active_id": project_id})


def project_info_for_cwd(cwd: str, *, db=None) -> dict[str, Any] | None:
    if not str(cwd or "").strip():
        return None
    store = db or _store()
    project = store.projects.project_for_path(cwd)
    if project is None:
        return None
    return {
        "id": project.id,
        "slug": project.slug,
        "name": project.name,
        "primary_path": project.primary_path,
    }


def _refresh_live_project_contexts(db) -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
    for session in sessions:
        cwd = str(session.get("cwd") or "").strip()
        if cwd:
            session["project"] = project_info_for_cwd(cwd, db=db)


@_project_method("projects.for_cwd")
def _for_cwd(rid, params: dict, db) -> dict:
    raw = str(params.get("cwd") or "").strip()
    cwd = _completion_cwd({"cwd": raw} if raw else params)
    project = db.projects.project_for_path(cwd)
    return _ok(
        rid,
        {
            "project": project.to_dict() if project else None,
            "cwd": cwd,
            "branch": git_probe.branch(cwd),
        },
    )


def _is_repo_junk(root: str) -> bool:
    if not root:
        return True
    real = os.path.normcase(os.path.realpath(root))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    hermes_home = os.path.normcase(os.path.realpath(str(get_hermes_home())))
    return real == home or real == hermes_home or real.startswith(hermes_home + os.sep)


def _is_session_cwd_junk(cwd: str) -> bool:
    if not cwd:
        return True
    real = os.path.normcase(os.path.realpath(cwd))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    hermes_home = os.path.normcase(os.path.realpath(str(get_hermes_home())))
    return real in {home, hermes_home}


def _discover_repos_payload(db, *, backfill: bool = True) -> list[dict[str, Any]]:
    repos: dict[str, dict[str, Any]] = {}

    def aggregate(root: str) -> dict[str, Any]:
        return repos.setdefault(
            root,
            {"root": root, "label": "", "sessions": 0, "last_active": 0.0},
        )

    cwd_rows = db.sessions.distinct_cwds()
    git_probe.warm_roots(str(row.get("cwd") or "") for row in cwd_rows)
    cwd_to_root: dict[str, str] = {}
    for row in cwd_rows:
        cwd = str(row.get("cwd") or "")
        root = git_probe.common_repo_root(cwd)
        if not root:
            continue
        cwd_to_root[cwd] = root
        if _is_repo_junk(root):
            continue
        item = aggregate(root)
        item["sessions"] += int(row.get("sessions") or 0)
        item["last_active"] = max(
            float(item["last_active"]),
            float(row.get("last_active") or 0),
        )
    if backfill:
        db.sessions.backfill_repo_roots(cwd_to_root)

    for entry in db.projects.list_discovered_repos():
        root = str(entry.get("root") or "")
        if not root or _is_repo_junk(root):
            continue
        item = aggregate(root)
        item["label"] = str(entry.get("label") or item["label"])
        item["last_active"] = max(
            float(item["last_active"]),
            float(entry.get("last_seen") or 0),
        )

    result = sorted(repos.values(), key=lambda item: item["last_active"], reverse=True)
    for item in result:
        item["label"] = (
            item["label"]
            or os.path.basename(str(item["root"]).rstrip("/\\"))
            or item["root"]
        )
    return result


@_project_method("projects.discover_repos")
def _discover_repos(rid, _params: dict, db) -> dict:
    return _ok(rid, {"repos": _discover_repos_payload(db)})


@_project_method("projects.record_repos")
def _record_repos(rid, params: dict, db) -> dict:
    pairs: list[tuple[str, str | None]] = []
    for item in params.get("repos") or []:
        if isinstance(item, str):
            pairs.append((item, None))
        elif isinstance(item, dict) and item.get("root"):
            pairs.append((str(item["root"]), item.get("label")))
    db.projects.record_discovered_repos(pairs, replace=True)
    return _ok(rid, {"repos": _discover_repos_payload(db)})


def _project_tree_row(row: dict) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "_lineage_root_id",
            "parent_session_id",
            "title",
            "preview",
            "started_at",
            "ended_at",
            "last_active",
            "source",
            "archived",
            "message_count",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
            "model",
            "cwd",
            "git_branch",
            "git_repo_root",
        )
    } | {"is_active": False}


def _build_tree(
    db,
    *,
    preview_limit: int,
    hydrate: bool,
    session_limit: int,
    include_discovered: bool,
) -> tuple[dict[str, Any], str | None]:
    rows = db.sessions.list_rich(
        limit=max(1, min(session_limit, 20_000)),
        offset=0,
        order_by_last_active=True,
        min_message_count=1,
        include_children=False,
        exclude_sources=_PROJECT_TREE_EXCLUDED_SOURCES,
        archived="false",
    )
    sessions = [_project_tree_row(row) for row in rows]
    git_probe.warm_roots(str(row.get("cwd") or "") for row in sessions)
    projects = [project.to_dict() for project in db.projects.list()]
    discovered = _discover_repos_payload(db, backfill=False) if include_discovered else []
    tree = project_tree.build_tree(
        projects,
        sessions,
        discovered,
        git_probe.resolve,
        preview_limit=max(0, preview_limit),
        hydrate=hydrate,
        is_junk_root=_is_repo_junk,
        is_junk_cwd=_is_session_cwd_junk,
    )
    return tree, db.projects.active_id()


@_project_method("projects.tree")
def _tree(rid, params: dict, db) -> dict:
    tree, active_id = _build_tree(
        db,
        preview_limit=int(params.get("preview_limit") or 3),
        hydrate=False,
        session_limit=int(params.get("session_limit") or 2000),
        include_discovered=True,
    )
    return _ok(rid, {**tree, "active_id": active_id})


@_project_method("projects.project_sessions")
def _project_sessions(rid, params: dict, db) -> dict:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("project_id required")
    tree, _active_id = _build_tree(
        db,
        preview_limit=0,
        hydrate=True,
        session_limit=int(params.get("session_limit") or 5000),
        include_discovered=False,
    )
    project = next(
        (item for item in tree["projects"] if item.get("id") == project_id),
        None,
    )
    return _ok(rid, {"project": project})


__all__ = ["project_info_for_cwd"]
