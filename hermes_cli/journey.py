"""Terminal access to the canonical Hermes learning journey."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _payload() -> dict[str, Any]:
    from agent.learning_graph import build_learning_graph

    return build_learning_graph()


def _date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).astimezone().strftime("%Y-%m-%d")
    except (OSError, OverflowError, TypeError, ValueError):
        return "unknown"


def _show(args: argparse.Namespace) -> int:
    graph = _payload()
    if getattr(args, "json", False):
        print(json.dumps(graph, ensure_ascii=False, indent=2))
        return 0
    timeline = list(graph.get("timeline") or [])
    if not timeline:
        print("No learning yet. Memories and learned skills will appear here over time.")
        return 0
    print("Hermes Journey — learned skills and memories over time")
    previous_date = ""
    for item in timeline:
        date = _date(item.get("timestamp"))
        if date != previous_date:
            print(f"\n{date}")
            previous_date = date
        glyph = "◆" if item.get("kind") == "memory" else "●"
        print(f"  {glyph} {item.get('label', '')}  [{item.get('nodeId', '')}]")
    stats = graph.get("stats") or {}
    print(
        "\n"
        f"{stats.get('memory_nodes', 0)} memories · "
        f"{stats.get('learned_skills', 0)} learned skills · "
        f"{len(graph.get('edges') or [])} connections"
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    nodes = sorted(
        _payload().get("nodes") or [],
        key=lambda node: (int(node.get("timestamp") or 0), str(node.get("id") or "")),
    )
    if getattr(args, "json", False):
        print(json.dumps(nodes, ensure_ascii=False, indent=2))
        return 0
    for node in nodes:
        glyph = "◆" if node.get("kind") == "memory" else "●"
        print(f"{node['id']}\t{glyph} {node.get('label', '')}\t{_date(node.get('timestamp'))}")
    return 0


def _delete(args: argparse.Namespace) -> int:
    from agent.learning_mutations import delete_node, node_detail

    detail = node_detail(args.node)
    if not detail.get("ok"):
        print(detail.get("message", "learning node not found"))
        return 1
    if not getattr(args, "yes", False):
        try:
            confirmed = input(f"Delete {detail['label']!r}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if confirmed not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    result = delete_node(args.node)
    print(result.get("message", "delete failed"))
    return 0 if result.get("ok") else 1


def _edit(args: argparse.Namespace) -> int:
    from agent.learning_mutations import edit_node, node_detail

    detail = node_detail(args.node)
    if not detail.get("ok"):
        print(detail.get("message", "learning node not found"))
        return 1
    replacement = getattr(args, "content", None)
    if replacement is None:
        suffix = ".md" if detail.get("kind") == "skill" else ".txt"
        replacement = _open_editor(str(detail.get("content") or ""), suffix=suffix)
    if replacement is None or replacement.strip() == str(detail.get("content") or "").strip():
        print("No changes.")
        return 0
    result = edit_node(args.node, replacement)
    print(result.get("message", "edit failed"))
    return 0 if result.get("ok") else 1


def _open_editor(initial: str, *, suffix: str) -> Optional[str]:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8"
        ) as handle:
            handle.write(initial)
            path = handle.name
        subprocess.run([*editor.split(), path], check=False)
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Editor failed: {exc}")
        return None
    finally:
        if path:
            try:
                Path(path).unlink()
            except OSError:
                pass


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print the graph payload as JSON.")
    parser.set_defaults(func=_show)
    actions = parser.add_subparsers(dest="journey_action")

    listing = actions.add_parser("list", help="List node ids for detail/edit/delete.")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=_list)

    editing = actions.add_parser("edit", help="Edit a memory or learned skill.")
    editing.add_argument("node")
    editing.add_argument("--content", help="Replacement content; otherwise open $EDITOR.")
    editing.set_defaults(func=_edit)

    deleting = actions.add_parser("delete", help="Delete a memory or archive a learned skill.")
    deleting.add_argument("node")
    deleting.add_argument("-y", "--yes", action="store_true")
    deleting.set_defaults(func=_delete)


def cmd_journey(args: argparse.Namespace) -> int:
    return _show(args)
