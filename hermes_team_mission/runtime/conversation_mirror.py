from __future__ import annotations

import json
import time
from typing import Any

from hermes_runtime_event_payloads import primary_deliverable_text
from hermes_team_mission.context.artifact_refs import artifact_refs_from_event
from hermes_team_mission.context.artifact_refs import dedupe_artifact_refs
from hermes_team_mission.domain.node_kinds import is_team_mission_synthesis_node_kind
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind
from hermes_team_mission.state import deliverables as _deliverable_state


_CONTROL_MIRROR_EVENT_TYPES = {
    "mission.approval.requested",
    "mission.strategy.actions",
}

_FINAL_DELIVERABLE_STREAM_EVENT_TYPES = {"message.start", "message.delta", "message.complete"}
_FINAL_DELIVERABLE_PERSISTED_MESSAGE_EVENT_TYPES = {"message.complete"}
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
    replace_existing_content: bool = False,
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
                if replace_existing_content and str(message.get("content") or "") != content:
                    return _replace_message_content(
                        db,
                        session_id=session_id,
                        message_id=message.get("id"),
                        content=content,
                    )
                return False
    except Exception:
        pass
    try:
        db.append_message(session_id, role, content, metadata=metadata)
        return True
    except Exception:
        return False


def _replace_message_content(
    db: Any,
    *,
    session_id: str,
    message_id: Any,
    content: str,
) -> bool:
    try:
        numeric_message_id = int(message_id)
    except (TypeError, ValueError):
        return False
    try:
        stored_content = db._encode_content(content) if hasattr(db, "_encode_content") else content

        def _do(conn: Any) -> bool:
            cursor = conn.execute(
                "UPDATE messages SET content = ? WHERE id = ? AND session_id = ?",
                (stored_content, numeric_message_id, session_id),
            )
            return bool(cursor.rowcount)

        if hasattr(db, "_execute_write"):
            return bool(db._execute_write(_do))
    except Exception:
        return False
    return False


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any) -> Any:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


def _payload_stream_text(payload: dict[str, Any]) -> str:
    for key in ("delta", "text", "snapshot"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return ""


def _stream_text_from_events(events: list[dict[str, Any]]) -> str:
    content = ""
    for event in events or []:
        if not isinstance(event, dict) or text(event.get("type")) != "message.delta":
            continue
        payload = _event_payload(event)
        chunk = _payload_stream_text(payload)
        if not chunk:
            continue
        mode = text(payload.get("mode")).lower()
        if mode == "snapshot" or payload.get("snapshot") is not None:
            content = chunk
        else:
            content += chunk
    return text(content)


def _final_deliverable_text_from_history(
    db: Any,
    *,
    target_session_id: str,
    conversation_run_id: str,
    source_session_id: str,
    source_run_id: str,
    prefer_source: bool = False,
) -> str:
    target_candidate = (target_session_id, conversation_run_id)
    source_candidate = (source_session_id, source_run_id)
    candidates = (
        (source_candidate, target_candidate)
        if prefer_source
        else (target_candidate, source_candidate)
    )
    for session_id, run_id in candidates:
        if not text(session_id) or not text(run_id):
            continue
        try:
            events = db.list_run_events(text(session_id), run_id=text(run_id), limit=5000)
        except Exception:
            events = []
        content = _stream_text_from_events(events)
        if content:
            return content
    return ""


def _node_by_id(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and text(node.get("node_id") or node.get("id")) == node_id:
            return dict(node)
    return {}


def _is_final_deliverable_node(node: dict[str, Any], binding: dict[str, Any]) -> bool:
    node = node if isinstance(node, dict) else {}
    binding = binding if isinstance(binding, dict) else {}
    metadata = mapping(node.get("metadata"))
    node_kind = normalize_team_mission_node_kind(node.get("kind"))
    phase = text(metadata.get("phase")).lower()
    node_role = text(metadata.get("role")).lower()
    binding_role = text(binding.get("role")).lower()
    final_roles = {"synthesis", "synthesizer", "finalizer"}
    return (
        is_team_mission_synthesis_node_kind(node_kind)
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


def _conversation_id_from_mission(mission: dict[str, Any]) -> str:
    metadata = mapping(mission.get("metadata"))
    return text(
        mission.get("conversation_id")
        or metadata.get("conversation_id")
        or metadata.get("conversationId")
        or metadata.get("team_conversation_id")
        or metadata.get("teamConversationId")
    )


def _event_identity_payload(
    *,
    mission: dict[str, Any],
    mission_id: str,
    target_session_id: str,
    node: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any]:
    node_id = text(node.get("node_id") or binding.get("node_id"))
    task_id = _task_id_from_metadata(
        node.get("metadata") if isinstance(node, dict) else {},
        binding.get("metadata") if isinstance(binding, dict) else {},
        mission.get("metadata") if isinstance(mission, dict) else {},
    ) or mission_id
    task_frame_id = f"mission-frame:{mission_id}" if mission_id else ""
    conversation_id = _conversation_id_from_mission(mission)
    return {
        "mission_id": mission_id,
        "missionId": mission_id,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "stable_session_id": target_session_id,
        "stableSessionId": target_session_id,
        "node_id": node_id,
        "nodeId": node_id,
        "task_id": task_id,
        "taskId": task_id,
        "task_frame_id": task_frame_id,
        "taskFrameId": task_frame_id,
    }


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
    artifact_refs: list[dict[str, Any]] | None = None,
) -> bool:
    task_id = _task_id_from_metadata(
        node.get("metadata") if isinstance(node, dict) else {},
        binding.get("metadata") if isinstance(binding, dict) else {},
    )
    task_frame_id = f"mission-frame:{mission_id}" if text(mission_id) else ""
    artifacts = dedupe_artifact_refs(artifact_refs or [])
    team_ref = {
        "kind": "final_deliverable",
        "mission_id": mission_id,
        "node_id": node_id,
        "node_kind": text(node.get("kind")),
        "task_id": task_id,
        "task_frame_id": task_frame_id,
        "conversation_session_id": target_session_id,
        "stable_session_id": target_session_id,
        "source_run_id": source_run_id,
        "source_session_id": source_session_id,
        "source_seq": source_seq,
    }
    if artifacts:
        team_ref["artifact_refs"] = artifacts
        team_ref["artifactRefs"] = artifacts
    return _append_message_once(
        db,
        session_id=target_session_id,
        role="assistant",
        content=text(content),
        metadata={
            **({"run_id": text(conversation_run_id)} if text(conversation_run_id) else {}),
            **({"turn_id": text(turn_id)} if text(turn_id) else {}),
            **({"client_message_id": text(client_message_id)} if text(client_message_id) else {}),
            "team_mission": team_ref,
        },
        replace_existing_content=True,
    )


def _upsert_legacy_imported_deliverable(
    db: Any,
    *,
    mission_id: str,
    source_run_id: str,
    node_id: str,
    node: dict[str, Any],
    binding: dict[str, Any],
    content: str,
    artifact_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not text(mission_id) or not text(source_run_id) or not text(node_id) or not text(content):
        return {}
    latest = {}
    getter = getattr(db, "latest_team_mission_deliverable_for_run", None)
    if callable(getter):
        try:
            latest = getter(source_run_id) or {}
        except Exception:
            latest = {}
    if isinstance(latest, dict) and text(latest.get("source")) in _deliverable_state.DELIVERABLE_EFFECTIVE_SOURCES:
        return latest
    task_id = _task_id_from_metadata(
        node.get("metadata") if isinstance(node, dict) else {},
        binding.get("metadata") if isinstance(binding, dict) else {},
    )
    summary = text(content)
    if len(summary) > 1600:
        summary = summary[:1597].rstrip() + "..."
    upsert = getattr(db, "upsert_team_mission_deliverable", None)
    if not callable(upsert):
        return {}
    return upsert(
        mission_id=mission_id,
        node_id=node_id,
        run_id=source_run_id,
        task_id=task_id,
        status="completed",
        result="PASS",
        summary=summary,
        payload={
            "node_id": node_id,
            "status": "completed",
            "result": "PASS",
            "summary": summary,
            "legacy_import_source": "final_deliverable_message",
        },
        artifact_refs=dedupe_artifact_refs(artifact_refs or []),
        next_context={},
        output_contract=dict(node.get("output_contract") or {}),
        source=_deliverable_state.DELIVERABLE_SOURCE_LEGACY_IMPORTED,
        confidence=0.7,
        visibility=_deliverable_state.DELIVERABLE_VISIBILITY_HANDOFF,
    )


def recover_final_deliverable_messages(db: Any, conversation: dict[str, Any] | None) -> int:
    if not isinstance(conversation, dict):
        return 0
    target_session_id = text(
        conversation.get("stable_session_id")
        or conversation.get("stableSessionId")
    )
    if not target_session_id:
        return 0
    try:
        events = db.list_run_events(target_session_id, limit=5000)
    except Exception:
        return 0
    recovered = 0
    for event in events or []:
        if not isinstance(event, dict) or text(event.get("type")) != "message.complete":
            continue
        payload = _event_payload(event)
        if not payload.get("team_mission_conversation_mirror"):
            continue
        if not payload.get("team_mission_final_deliverable"):
            continue
        mission_id = text(payload.get("mission_id"))
        source_run_id = text(payload.get("source_run_id"))
        source_session_id = text(payload.get("source_session_id"))
        if not mission_id or not source_run_id:
            continue
        conversation_run_id = text(event.get("run_id") or payload.get("run_id")) or _mirror_run_id(mission_id, source_run_id)
        final_deliverable_text = _final_deliverable_text_from_history(
            db,
            target_session_id=target_session_id,
            conversation_run_id=conversation_run_id,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
            prefer_source=True,
        ) or primary_deliverable_text(payload)
        if not final_deliverable_text:
            continue
        graph = db.get_team_mission_graph(mission_id)
        graph = graph if isinstance(graph, dict) else {}
        node_id = text(payload.get("node_id"))
        node = _node_by_id(graph, node_id)
        try:
            binding = db.get_team_mission_run_binding(source_run_id)
        except Exception:
            binding = {}
        if _append_final_deliverable_message(
            db,
            mission_id=mission_id,
            target_session_id=target_session_id,
            conversation_run_id=conversation_run_id,
            source_run_id=source_run_id,
            source_session_id=source_session_id,
            source_seq=text(payload.get("source_seq")),
            turn_id=text(event.get("turn_id") or payload.get("turn_id")),
            client_message_id=text(event.get("client_message_id") or payload.get("client_message_id")),
            node_id=node_id,
            node=node,
            binding=dict(binding or {}),
            content=final_deliverable_text,
            artifact_refs=artifact_refs_from_event(event),
        ):
            _upsert_legacy_imported_deliverable(
                db,
                mission_id=mission_id,
                source_run_id=source_run_id,
                node_id=node_id,
                node=node,
                binding=dict(binding or {}),
                content=final_deliverable_text,
                artifact_refs=artifact_refs_from_event(event),
            )
            recovered += 1
    return recovered


def _is_final_deliverable_message(
    *,
    event_type: str,
    payload: dict[str, Any],
    node: dict[str, Any],
    binding: dict[str, Any],
) -> bool:
    if event_type not in _FINAL_DELIVERABLE_STREAM_EVENT_TYPES:
        return False
    if event_type == "message.delta" and not _payload_stream_text(payload):
        return False
    if event_type == "message.complete":
        return _is_final_deliverable_node(node, binding)
    payload_status = text(payload.get("status")).lower()
    if payload_status in _NON_DELIVERABLE_MESSAGE_STATUSES:
        if event_type == "message.complete" and payload_status in {"failed", "error"}:
            return _is_final_deliverable_node(node, binding)
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
    if event_type not in _CONTROL_MIRROR_EVENT_TYPES and event_type not in _FINAL_DELIVERABLE_STREAM_EVENT_TYPES:
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
    identity_payload = _event_identity_payload(
        mission=mission,
        mission_id=mission_id,
        target_session_id=target_session_id,
        node=node,
        binding=binding,
    )
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
    should_persist_final_message = (
        is_final_deliverable
        and event_type in _FINAL_DELIVERABLE_PERSISTED_MESSAGE_EVENT_TYPES
    )
    final_deliverable_text = ""
    if is_final_deliverable and event_type == "message.complete":
        final_deliverable_text = primary_deliverable_text(payload) or _final_deliverable_text_from_history(
            db,
            target_session_id=target_session_id,
            conversation_run_id=mirror_run_id,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
            prefer_source=True,
        )
    if final_deliverable_text and event_type == "message.complete":
        payload["text"] = final_deliverable_text
    if event_type == "message.delta":
        stream_text = _payload_stream_text(payload)
        if stream_text and not text(payload.get("mode")):
            payload["mode"] = "append"
        if text(payload.get("mode")).lower() == "append":
            payload["delta"] = stream_text
            payload["text"] = stream_text
            payload.pop("snapshot", None)
    if (
        is_final_deliverable
        and event_type == "message.complete"
        and text(payload.get("status")).lower() in {"failed", "error"}
    ):
        original_status = text(payload.get("status"))
        original_error = text(payload.get("message") or payload.get("error"))
        payload["status"] = "complete"
        payload["team_mission_terminal_status_recovered"] = original_status
        if original_error:
            payload["nonfatal_error"] = original_error
    if already_mirrored:
        if should_persist_final_message:
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
                content=final_deliverable_text,
                artifact_refs=artifact_refs_from_event(frame),
            )
        return {}
    payload.update(
        {
            "run_id": mirror_run_id,
            "source_run_id": source_run_id,
            "source_seq": source_seq,
            "source_session_id": source_session_id,
            "source_runtime_scope_key": text(frame.get("runtime_scope_key") or binding.get("runtime_scope_key")),
            **identity_payload,
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
        **{key: value for key, value in identity_payload.items() if "_" in key and text(value)},
        "source_seq": source_seq,
        "seq": 0,
        "timestamp": float(frame.get("timestamp") or time.time()),
    }
    try:
        if not db.get_session(target_session_id):
            db.create_session(target_session_id, source="team_mission", transient=False)
    except Exception:
        pass
    if should_persist_final_message:
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
            content=final_deliverable_text,
            artifact_refs=artifact_refs_from_event(mirror),
        )
    saved = db.append_run_event(target_session_id, mirror)
    if event_type == "message.delta" and isinstance(saved, dict):
        return {
            **mirror,
            "seq": saved.get("seq") or mirror.get("seq") or 0,
            "timestamp": saved.get("timestamp") or mirror.get("timestamp") or time.time(),
        }
    return saved if isinstance(saved, dict) else {}
