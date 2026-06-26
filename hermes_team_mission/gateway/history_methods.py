# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals
from hermes_team_mission.runtime.history import (
    get_team_mission_node_runtime_history,
)

_server = bind_server_globals(globals())


@method("team_mission.node.history")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    result = get_team_mission_node_runtime_history(db, params or {})
    if isinstance(result, dict) and result.get("error"):
        return _err(rid, int(result.get("code") or 5008), str(result.get("error") or "team mission node history unavailable"))
    return _ok(rid, result)
