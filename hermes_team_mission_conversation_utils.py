from __future__ import annotations

import time
from typing import Any


_CONTROL_MIRROR_EVENT_TYPES = {
    "mission.approval.requested",
    "mission.strategy.actions",
}

_FINAL_DELIVERABLE_MESSAGE_EVENT_TYPES = {"message.complete"}
_NON_DELIVERABLE_MESSAGE_STATUSES = {
    "cancelled",
    "canceled",
    "failed",
    "error",
    "interrupted",
}


def text(value: Any) -> str:
    return str(value or "").strip()


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def conversation_session_id(mission: dict[str, Any] | None) -> str:
    mission = mission if isinstance(mission, dict) else {}
    metadata = mapping(mission.get("metadata"))
    return text(
        metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("stable_team_session_id")
        or metadata.get("stableTeamSessionId")
        or metadata.get("team_session_id")
        or metadata.get("teamSessionId")
        or mission.get("leader_session_id")
        or mission.get("team_id")
        or mission.get("mission_id")
    )


def _metadata_matches(
    message: dict[str, Any],
    *,
    mission_id: str,
    node_id: str,
    kind: str,
    source_run_id: str,
) -> bool:
    metadata = mapping(message.get("metadata"))
    team = mapping(metadata.get("team_mission"))
    if text(team.get("kind")) != kind:
        return False
    if text(team.get("mission_id")) != mission_id:
        return False
    if source_run_id:
        return text(team.get("source_run_id") or team.get("run_id")) == source_run_id
    if node_id and text(team.get("node_id")) != node_id:
        return False
    return True


def _append_message_once(
    db: Any,
    *,
    session_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any],
) -> bool:
    if not session_id or not content:
        return False
    try:
        if not db.get_session(session_id):
            db.create_session(session_id, source="team_mission", transient=False)
    except Exception:
        pass
    try:
        mission = mapping(metadata.get("team_mission"))
        mission_id = text(mission.get("mission_id"))
        node_id = text(mission.get("node_id"))
        kind = text(mission.get("kind"))
        source_run_id = text(mission.get("source_run_id") or mission.get("run_id"))
        for message in db.get_messages(session_id):
            if (
                isinstance(message, dict)
                and text(message.get("role")) == role
                and _metadata_matches(
                    message,
                    mission_id=mission_id,
                    node_id=node_id,
                    kind=kind,
                    source_run_id=source_run_id,
                )
            ):
                return False
    except Exception:
        pass
    try:
        db.append_message(session_id, role, content, metadata=metadata)
        return True
    except Exception:
        return False


def append_user_task_message(
    db: Any,
    *,
    mission: dict[str, Any],
    objective: str,
    node_id: str = "",
    task_id: str = "",
) -> bool:
    session_id = conversation_session_id(mission)
    mission_id = text((mission or {}).get("mission_id"))
    metadata = {
        "team_mission": {
            "kind": "user_task",
            "mission_id": mission_id,
            "node_id": text(node_id),
            "task_id": text(task_id),
            "conversation_session_id": session_id,
        }
    }
    return _append_message_once(
        db,
        session_id=session_id,
        role="user",
        content=text(objective),
        metadata=metadata,
    )


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return dict(payload)


def _node_by_id(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and text(node.get("node_id") or node.get("id")) == node_id:
            return dict(node)
    return {}


def _is_final_deliverable_node(node: dict[str, Any], binding: dict[str, Any]) -> bool:
    node = node if isinstance(node, dict) else {}
    binding = binding if isinstance(binding, dict) else {}
    metadata = mapping(node.get("metadata"))
    node_kind = text(node.get("kind")).lower()
    phase = text(metadata.get("phase")).lower()
    node_role = text(metadata.get("role")).lower()
    binding_role = text(binding.get("role")).lower()
    final_roles = {"synthesis", "synthesizer", "finalizer"}
    return (
        node_kind in final_roles
        or phase == "synthesis"
        or node_role in final_roles
        or binding_role in final_roles
    )


def _task_id_from_metadata(*values: dict[str, Any]) -> str:
    for value in values:
        metadata = mapping(value)
        task_id = text(
            metadata.get("task_id")
            or metadata.get("taskId")
            or metadata.get("submitted_task_id")
            or metadata.get("submittedTaskId")
        )
        if task_id:
            return task_id
    return ""


def _append_final_deliverable_message(
    db: Any,
    *,
    mission_id: str,
    target_session_id: str,
    conversation_run_id: str,
    source_run_id: str,
    source_session_id: str,
    source_seq: str,
    turn_id: str,
    client_message_id: str,
    node_id: str,
    node: dict[str, Any],
    binding: dict[str, Any],
    content: str,
) -> bool:
    task_id = _task_id_from_metadata(
        node.get("metadata") if isinstance(node, dict) else {},
        binding.get("metadata") if isinstance(binding, dict) else {},
    )
    return _append_message_once(
        db,
        session_id=target_session_id,
        role="assistant",
        content=text(content),
        metadata={
            **({"run_id": text(conversation_run_id)} if text(conversation_run_id) else {}),
            **({"turn_id": text(turn_id)} if text(turn_id) else {}),
            **({"client_message_id": text(client_message_id)} if text(client_message_id) else {}),
            "team_mission": {
                "kind": "final_deliverable",
                "mission_id": mission_id,
                "node_id": node_id,
                "node_kind": text(node.get("kind")),
                "task_id": task_id,
                "conversation_session_id": target_session_id,
                "source_run_id": source_run_id,
                "source_session_id": source_session_id,
                "source_seq": source_seq,
            }
        },
    )


def _is_final_deliverable_message(
    *,
    event_type: str,
    payload: dict[str, Any],
    node: dict[str, Any],
    binding: dict[str, Any],
) -> bool:
    if event_type not in _FINAL_DELIVERABLE_MESSAGE_EVENT_TYPES:
        return False
    if not text(payload.get("text")):
        return False
    if text(payload.get("status")).lower() in _NON_DELIVERABLE_MESSAGE_STATUSES:
        return False
    return _is_final_deliverable_node(node, binding)


def _should_mirror_event(
    *,
    event_type: str,
    payload: dict[str, Any],
    node: dict[str, Any],
    binding: dict[str, Any],
) -> tuple[bool, bool]:
    if event_type in _CONTROL_MIRROR_EVENT_TYPES:
        return True, False
    if _is_final_deliverable_message(
        event_type=event_type,
        payload=payload,
        node=node,
        binding=binding,
    ):
        return True, True
    return False, False


def _already_mirrored_event(
    db: Any,
    *,
    session_id: str,
    event_type: str,
    mission_id: str,
    node_id: str,
    source_run_id: str,
    source_seq: str,
) -> bool:
    try:
        events = db.list_run_events(session_id)
    except Exception:
        return False
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if text(event.get("type")) != event_type:
            continue
        payload = mapping(event.get("payload"))
        if not payload.get("team_mission_conversation_mirror"):
            continue
        if text(payload.get("mission_id")) != mission_id:
            continue
        if source_run_id and text(payload.get("source_run_id")) != source_run_id:
            continue
        if node_id and text(payload.get("node_id")) != node_id:
            continue
        if source_seq and text(payload.get("source_seq")) != source_seq:
            continue
        return True
    return False


def _mirror_run_id(mission_id: str, run_id: str) -> str:
    return f"team-mission:{mission_id}:conversation:{run_id}"


def mirror_event_to_conversation(
    db: Any,
    *,
    mission_id: str,
    binding: dict[str, Any] | None = None,
    event: dict[str, Any],
    source: str = "",
    target_session_id: str = "",
) -> dict[str, Any]:
    frame = dict(event or {})
    event_type = text(frame.get("type"))
    if event_type not in _CONTROL_MIRROR_EVENT_TYPES and event_type not in _FINAL_DELIVERABLE_MESSAGE_EVENT_TYPES:
        return {}
    payload = _event_payload(frame)
    if payload.get("team_mission_conversation_mirror"):
        return {}
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return {}
    target_session_id = text(target_session_id) or conversation_session_id(mission)
    if not target_session_id:
        return {}
    source_run_id = text(frame.get("run_id") or payload.get("run_id"))
    if not source_run_id:
        return {}
    if binding is None and source_run_id:
        try:
            binding = db.get_team_mission_run_binding(source_run_id)
        except Exception:
            binding = {}
    binding = dict(binding or {})
    source_session_id = text(frame.get("stored_session_id") or binding.get("session_id"))
    if target_session_id == source_session_id:
        return {}
    node_id = text(payload.get("node_id") or binding.get("node_id"))
    node = _node_by_id(graph, node_id)
    should_mirror, is_final_deliverable = _should_mirror_event(
        event_type=event_type,
        payload=payload,
        node=node,
        binding=binding,
    )
    if not should_mirror:
        return {}
    source_seq = text(frame.get("source_seq") or frame.get("seq"))
    mirror_run_id = _mirror_run_id(mission_id, source_run_id)
    turn_id = text(frame.get("turn_id") or payload.get("turn_id"))
    client_message_id = text(
        frame.get("client_message_id")
        or frame.get("clientMessageId")
        or payload.get("client_message_id")
        or payload.get("clientMessageId")
    )
    already_mirrored = _already_mirrored_event(
        db,
        session_id=target_session_id,
        event_type=event_type,
        mission_id=mission_id,
        node_id=node_id,
        source_run_id=source_run_id,
        source_seq=source_seq,
    )
    if already_mirrored:
        if is_final_deliverable:
            _append_final_deliverable_message(
                db,
                mission_id=mission_id,
                target_session_id=target_session_id,
                conversation_run_id=mirror_run_id,
                source_run_id=source_run_id,
                source_session_id=source_session_id,
                source_seq=source_seq,
                turn_id=turn_id,
                client_message_id=client_message_id,
                node_id=node_id,
                node=node,
                binding=binding,
                content=text(payload.get("text")),
            )
        return {}
    payload.update(
        {
            "run_id": mirror_run_id,
            "source_run_id": source_run_id,
            "source_seq": source_seq,
            "source_session_id": source_session_id,
            "mission_id": mission_id,
            "node_id": node_id,
            "node_kind": text(node.get("kind")),
            "team_mission_final_deliverable": is_final_deliverable,
            "team_mission_conversation_mirror": True,
            "team_mission_mirror_source": text(source),
        }
    )
    mirror = {
        **frame,
        "stored_session_id": target_session_id,
        "run_id": mirror_run_id,
        "runtime_scope_key": f"team_mission:{mission_id}",
        "payload": payload,
        "seq": 0,
        "timestamp": float(frame.get("timestamp") or time.time()),
    }
    try:
        if not db.get_session(target_session_id):
            db.create_session(target_session_id, source="team_mission", transient=False)
    except Exception:
        pass
    if is_final_deliverable:
        _append_final_deliverable_message(
            db,
            mission_id=mission_id,
            target_session_id=target_session_id,
            conversation_run_id=mirror_run_id,
            source_run_id=source_run_id,
            source_session_id=source_session_id,
            source_seq=source_seq,
            turn_id=turn_id,
            client_message_id=client_message_id,
            node_id=node_id,
            node=node,
            binding=binding,
            content=text(payload.get("text")),
        )
    saved = db.append_run_event(target_session_id, mirror)
    return saved if isinstance(saved, dict) else {}
