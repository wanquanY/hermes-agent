from __future__ import annotations

from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())

_TEAM_WAITING_APPROVAL_STATUSES = {"waiting_approval"}
_TEAM_RUNNING_STATUSES = {
    "planning",
    "running",
    "partially_blocked",
    "blocked",
    "verifying",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on"}


def _bounded_limit(value: Any, default: int = 200, maximum: int = 500) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _session_id(session: dict[str, Any]) -> str:
    return _text(
        session.get("stored_session_id")
        or session.get("storedSessionId")
        or session.get("id")
        or session.get("session_id")
    )


def _mission_id(session: dict[str, Any]) -> str:
    return _text(
        session.get("mission_id")
        or session.get("missionId")
        or session.get("active_mission_id")
        or session.get("activeMissionId")
    )


def _session_kind(session: dict[str, Any]) -> str:
    source = _text(session.get("source")).lower()
    kind = _text(session.get("session_kind") or session.get("sessionKind")).lower()
    if source == "team_mission" or kind == "team_mission":
        return "team_mission"
    return "ordinary"


def _team_mission_from_session(session: dict[str, Any]) -> dict[str, Any]:
    mission_id = _mission_id(session)
    if not mission_id:
        return {}
    try:
        graph = _get_db().get_team_mission_graph(mission_id)
    except Exception:
        return {}
    if not isinstance(graph, dict):
        return {}
    mission = graph.get("mission")
    return mission if isinstance(mission, dict) else {}


def _mission_status(session: dict[str, Any], mission: dict[str, Any] | None = None) -> str:
    source = mission if isinstance(mission, dict) else {}
    return _text(
        source.get("status")
        or session.get("mission_status")
        or session.get("missionStatus")
        or session.get("status")
    )


def _pending_approvals_for_session(session_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if not session_id:
        return []
    handler = _methods.get("approval.pending.list")
    if not callable(handler):
        return []
    response = handler(
        "conversation-activity-approval",
        {
            **params,
            "stored_session_id": session_id,
        },
    )
    if not isinstance(response, dict) or response.get("error"):
        return []
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    approvals = result.get("approvals")
    return [item for item in approvals if isinstance(item, dict)] if isinstance(approvals, list) else []


def _run_state(
    session: dict[str, Any],
    *,
    mission: dict[str, Any] | None = None,
    pending_approval_count: int,
) -> str:
    status = _mission_status(session, mission)
    if pending_approval_count > 0 or status in _TEAM_WAITING_APPROVAL_STATUSES:
        return "waiting_approval"
    if _truthy(session.get("running")) or _text(session.get("active_run_id")):
        return "running"
    if _session_kind(session) == "team_mission" and status in _TEAM_RUNNING_STATUSES:
        return "running"
    if status in {"completed", "failed", "cancelled", "canceled"}:
        return "completed" if status == "completed" else status.replace("canceled", "cancelled")
    return "idle"


def _activity_from_session(
    session: dict[str, Any],
    *,
    pending_approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    session_id = _session_id(session)
    mission = _team_mission_from_session(session) if _session_kind(session) == "team_mission" else {}
    pending_approval_count = len(pending_approvals)
    run_state = _run_state(session, mission=mission, pending_approval_count=pending_approval_count)
    is_active = run_state in {"running", "waiting_approval"}
    return {
        "stable_session_id": session_id,
        "stored_session_id": session_id,
        "session_id": session_id,
        "conversation_id": _text(session.get("conversation_id") or session.get("conversationId")),
        "kind": _session_kind(session),
        "source": _text(session.get("source")),
        "run_state": run_state,
        "activity_state": run_state,
        "running": run_state == "running",
        "waiting_approval": run_state == "waiting_approval",
        "active_run_id": _text(session.get("active_run_id") or session.get("activeRunId")) if is_active else "",
        "active_turn_id": _text(session.get("active_turn_id") or session.get("activeTurnId")) if is_active else "",
        "active_runtime_session_id": (
            _text(
                session.get("active_runtime_session_id")
                or session.get("activeRuntimeSessionId")
                or session.get("runtime_session_id")
            )
            if is_active
            else ""
        ),
        "run_started_at": session.get("run_started_at") if is_active else 0,
        "run_updated_at": session.get("run_updated_at") or 0,
        "updated_at": session.get("updated_at") or 0,
        "team_id": _text(session.get("team_id") or session.get("teamId")),
        "mission_id": _mission_id(session),
        "mission_status": _mission_status(session, mission),
        "pending_approval_count": pending_approval_count,
        "pending_approvals": pending_approvals,
    }


@method("conversation.activity.list")
def _(rid, params: dict) -> dict:
    limit = _bounded_limit(params.get("limit"))
    session_handler = _methods.get("session.list")
    if not callable(session_handler):
        return _err(rid, 5008, "session.list unavailable")
    session_response = session_handler(
        "conversation-activity-sessions",
        {
            **params,
            "limit": limit,
        },
    )
    if not isinstance(session_response, dict):
        return _err(rid, 5008, "session.list returned invalid response")
    if session_response.get("error"):
        return session_response
    result = session_response.get("result") if isinstance(session_response.get("result"), dict) else {}
    sessions = result.get("sessions") if isinstance(result.get("sessions"), list) else []
    include_approvals = params.get("include_approvals")
    if include_approvals is None:
        include_approvals = params.get("includeApprovals")
    include_approvals = True if include_approvals is None else _truthy(include_approvals)
    activities = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = _session_id(session)
        pending_approvals = _pending_approvals_for_session(session_id, params) if include_approvals else []
        activities.append(_activity_from_session(session, pending_approvals=pending_approvals))
    return _ok(
        rid,
        {
            "activities": activities,
            "sessions": sessions,
            "pageInfo": result.get("pageInfo") if isinstance(result.get("pageInfo"), dict) else {"hasMore": False},
        },
    )
