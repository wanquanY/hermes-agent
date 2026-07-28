"""Profile-safe learning journey RPC methods for the TUI."""

from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.profile_home_scope import session_home_scope

bind_server_globals(globals())


@method("learning.frames")
def _(rid, params: dict) -> dict:
    try:
        cols = max(20, int(params.get("cols", 80) or 80))
        rows = max(10, int(params.get("rows", 24) or 24))
        frames = max(1, int(params.get("frames", 48) or 48))
    except (TypeError, ValueError):
        return _err(rid, 4004, "cols, rows, and frames must be integers")
    try:
        from agent.learning_graph import build_learning_graph
        from agent.learning_graph_render import render_frames

        with session_home_scope(_sessions, params):
            payload = build_learning_graph()
            rendered = render_frames(
                payload,
                cols=cols,
                rows=rows,
                frames=frames,
            )
        return _ok(rid, rendered)
    except Exception as exc:
        return _err(rid, 5000, f"learning.frames failed: {exc}")


@method("learning.detail")
def _(rid, params: dict) -> dict:
    try:
        from agent.learning_mutations import node_detail

        with session_home_scope(_sessions, params):
            return _ok(rid, node_detail(str(params.get("id", ""))))
    except Exception as exc:
        return _err(rid, 5000, f"learning.detail failed: {exc}")


@method("learning.delete")
def _(rid, params: dict) -> dict:
    try:
        from agent.learning_mutations import delete_node

        with session_home_scope(_sessions, params):
            return _ok(rid, delete_node(str(params.get("id", ""))))
    except Exception as exc:
        return _err(rid, 5000, f"learning.delete failed: {exc}")


@method("learning.edit")
def _(rid, params: dict) -> dict:
    try:
        from agent.learning_mutations import edit_node

        with session_home_scope(_sessions, params):
            result = edit_node(
                str(params.get("id", "")),
                str(params.get("content", "")),
            )
        return _ok(rid, result)
    except Exception as exc:
        return _err(rid, 5000, f"learning.edit failed: {exc}")


__all__ = []
