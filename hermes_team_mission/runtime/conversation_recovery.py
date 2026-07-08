from __future__ import annotations

from typing import Any

from hermes_team_mission.domain.statuses import terminal_run_status_for_mission
from hermes_team_mission.runtime.conversation_mirror import recover_legacy_final_deliverables
from tui_gateway.services import run_control


def _text(value: Any) -> str:
    return str(value or "").strip()


def _terminal_run_status(mission_status: str) -> str:
    return terminal_run_status_for_mission(mission_status)


def recover_conversation_active_run(
    db: Any,
    conversation: dict | None,
    *,
    current_gateway_instance_id: str = "",
) -> dict[str, Any]:
    if not isinstance(conversation, dict):
        return {}
    conversation_session_id = _text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
    )
    if not conversation_session_id:
        return {}
    try:
        recover_legacy_final_deliverables(db, conversation)
    except Exception:
        pass
    run_state = run_control.session_status(
        conversation_session_id,
        db=db,
        current_gateway_instance_id=current_gateway_instance_id,
    )
    if not bool(run_state.get("running")):
        return run_state
    mission_id = _text(
        conversation.get("active_mission_id")
        or conversation.get("activeMissionId")
        or conversation.get("mission_id")
        or conversation.get("missionId")
    )
    if not mission_id:
        return run_state
    graph = db.get_team_mission_graph(mission_id) if hasattr(db, "get_team_mission_graph") else {}
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    mission = mission if isinstance(mission, dict) else {}
    terminal_status = _terminal_run_status(mission.get("status") or conversation.get("status"))
    if not terminal_status:
        return run_state
    active_run_id = _text(run_state.get("active_run_id"))
    mirror_run_prefix = f"team-mission:{mission_id}:conversation:"
    if not active_run_id.startswith(mirror_run_prefix):
        return run_state
    expected_scope = f"team_mission:{mission_id}"
    runtime_scope_key = _text(run_state.get("runtime_scope_key"))
    if runtime_scope_key and runtime_scope_key != expected_scope:
        return run_state
    run_control.publish_run_terminal_event(
        conversation_session_id=conversation_session_id,
        run_id=active_run_id,
        turn_id=_text(run_state.get("active_turn_id")),
        runtime_scope_key=runtime_scope_key or expected_scope,
        status=terminal_status,
        message="",
        db=db,
    )
    return {
        **run_state,
        "running": False,
        "active_run_id": "",
        "active_turn_id": "",
        "runtime_scope_key": "",
        "recovered_terminal_mirror_run_id": active_run_id,
    }
