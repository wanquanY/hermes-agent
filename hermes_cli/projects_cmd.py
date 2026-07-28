"""``hermes project`` commands over the canonical project application service."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import sys
from typing import Iterator

from hermes_agent.composition.cli_session_store import (
    CliSessionStore,
    open_cli_session_store,
)


@contextmanager
def _store() -> Iterator[CliSessionStore]:
    store = open_cli_session_store()
    try:
        yield store
    finally:
        store.close()


def build_parser(
    parent_subparsers: argparse._SubParsersAction,
) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "project",
        help="Manage projects (named, multi-folder workspaces)",
        description=(
            "Projects are per-profile workspaces spanning one or more folders. "
            "They anchor session grouping and optional kanban board defaults."
        ),
    )
    sub = parser.add_subparsers(dest="project_action")

    create = sub.add_parser("create", help="Create a new project")
    create.add_argument("name")
    create.add_argument("folders", nargs="*")
    create.add_argument("--slug", default=None)
    create.add_argument("--primary", default=None, metavar="PATH")
    create.add_argument("--description", default=None)
    create.add_argument("--icon", default=None)
    create.add_argument("--color", default=None)
    create.add_argument("--board", default=None, metavar="SLUG")
    create.add_argument("--use", action="store_true")

    listing = sub.add_parser("list", aliases=["ls"], help="List projects")
    listing.add_argument("--all", action="store_true", dest="include_archived")

    show = sub.add_parser("show", help="Show a project")
    show.add_argument("project")

    add = sub.add_parser("add-folder", help="Add a folder")
    add.add_argument("project")
    add.add_argument("path")
    add.add_argument("--label", default=None)
    add.add_argument("--primary", action="store_true")

    remove = sub.add_parser("remove-folder", help="Remove a folder")
    remove.add_argument("project")
    remove.add_argument("path")

    rename = sub.add_parser("rename", help="Rename a project")
    rename.add_argument("project")
    rename.add_argument("name")

    primary = sub.add_parser("set-primary", help="Set the primary folder")
    primary.add_argument("project")
    primary.add_argument("path")

    use = sub.add_parser("use", help="Set the active project")
    use.add_argument("project", nargs="?", default=None)

    for action in ("archive", "restore"):
        action_parser = sub.add_parser(action, help=f"{action.title()} a project")
        action_parser.add_argument("project")

    board = sub.add_parser("bind-board", help="Bind or unbind a kanban board")
    board.add_argument("project")
    board.add_argument("board", nargs="?", default="")

    parser.set_defaults(_project_parser=parser)
    return parser


def _print(project) -> None:
    flags = " (archived)" if project.archived else ""
    print(f"{project.slug}  [{project.id}]{flags}")
    print(f"  name:    {project.name}")
    if project.description:
        print(f"  about:   {project.description}")
    if project.board_slug:
        print(f"  board:   {project.board_slug}")
    if project.primary_path:
        print(f"  primary: {project.primary_path}")
    if project.folders:
        print("  folders:")
        for folder in project.folders:
            mark = " *" if folder.is_primary else "  "
            label = f" ({folder.label})" if folder.label else ""
            print(f"   {mark} {folder.path}{label}")


def _require(store: CliSessionStore, identity: str):
    project = store.projects.get(identity)
    if project is None:
        raise LookupError(f"no such project: {identity}")
    return project


def _sync_board_default_workdir(project, board_slug: str) -> None:
    if not project.primary_path or not board_slug.strip():
        return
    try:
        from hermes_cli import kanban_db

        slug = kanban_db._normalize_board_slug(board_slug)
        if not slug:
            return
        if slug != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(slug):
            return
        kanban_db.write_board_metadata(slug, default_workdir=project.primary_path)
    except Exception:
        return


def projects_command(args: argparse.Namespace) -> int:
    action = getattr(args, "project_action", None)
    if not action:
        getattr(args, "_project_parser").print_help()
        return 0
    try:
        with _store() as store:
            if action == "create":
                project = store.projects.create(
                    name=args.name,
                    slug=args.slug,
                    folders=args.folders,
                    primary_path=args.primary,
                    description=args.description,
                    icon=args.icon,
                    color=args.color,
                    board_slug=args.board,
                )
                if args.use:
                    store.projects.set_active(project.id)
                print(f"Created project {project.slug} ({project.id})")
                _print(project)
                return 0
            if action in {"list", "ls"}:
                active_id = store.projects.active_id()
                projects = store.projects.list(
                    include_archived=bool(args.include_archived)
                )
                if not projects:
                    print("No projects yet. Create one with `hermes project create <name>`.")
                    return 0
                for project in projects:
                    marker = "*" if project.id == active_id else " "
                    flags = " (archived)" if project.archived else ""
                    print(
                        f"{marker} {project.slug:<24} {project.name}{flags}  "
                        f"[{len(project.folders)} folder(s)]"
                    )
                return 0
            if action == "use":
                if not args.project:
                    store.projects.set_active(None)
                    print("Cleared active project")
                    return 0
                project = _require(store, args.project)
                store.projects.set_active(project.id)
                print(f"Active project: {project.slug}")
                return 0

            project = _require(store, args.project)
            if action == "show":
                _print(project)
            elif action == "add-folder":
                project = store.projects.add_folder(
                    project.id,
                    args.path,
                    label=args.label,
                    is_primary=args.primary,
                )
                print(f"Added {args.path} to {project.slug}")
            elif action == "remove-folder":
                project = store.projects.remove_folder(project.id, args.path)
                print(f"Removed {args.path} from {project.slug}")
            elif action == "rename":
                project = store.projects.update(project.id, name=args.name)
                print(f"Renamed {project.slug} -> {project.name}")
            elif action == "set-primary":
                project = store.projects.set_primary(project.id, args.path)
                print(f"Set primary of {project.slug} -> {project.primary_path}")
            elif action in {"archive", "restore"}:
                project = store.projects.set_archived(
                    project.id,
                    action == "archive",
                )
                print(f"{action.title()}d {project.slug}")
            elif action == "bind-board":
                project = store.projects.update(project.id, board_slug=args.board)
                if project.board_slug:
                    print(f"Bound {project.slug} -> board {project.board_slug}")
                    _sync_board_default_workdir(project, project.board_slug)
                else:
                    print(f"Unbound board from {project.slug}")
            else:
                print(f"Unknown project action: {action}", file=sys.stderr)
                return 1
            return 0
    except LookupError as exc:
        print(f"project: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"project: {exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "projects_command"]
