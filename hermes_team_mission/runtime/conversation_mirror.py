from __future__ import annotations

import json
from typing import Any

from hermes_runtime_event_payloads import primary_deliverable_text
from hermes_team_mission.context.artifact_refs import artifact_refs_from_event
from hermes_team_mission.context.artifact_refs import dedupe_artifact_refs
from hermes_team_mission.state import deliverables as _deliverable_state

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
        or metadata.get("conversation_team_session_id")
        or metadata.get("conversationTeamSessionId")
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
    participant_id: str = "",
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
        db.append_message(
            session_id,
            role,
            content,
            participant_id=participant_id,
            metadata=metadata,
        )
        return True
    except Exception:
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


def recover_legacy_final_deliverables(db: Any, conversation: dict[str, Any] | None) -> int:
    if not isinstance(conversation, dict):
        return 0
    target_session_id = text(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
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
        final_deliverable_text = _final_deliverable_text_from_history(
            db,
            target_session_id=target_session_id,
            conversation_run_id=text(event.get("run_id") or payload.get("run_id")) or _mirror_run_id(mission_id, source_run_id),
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
        if _upsert_legacy_imported_deliverable(
            db,
            mission_id=mission_id,
            source_run_id=source_run_id,
            node_id=node_id,
            node=node,
            binding=dict(binding or {}),
            content=final_deliverable_text,
            artifact_refs=artifact_refs_from_event(event),
        ):
            recovered += 1
    return recovered


def _mirror_run_id(mission_id: str, run_id: str) -> str:
    return f"team-mission:{mission_id}:conversation:{run_id}"


def _emit_mirror_diagnostic(stage: str, **fields: Any) -> None:
    try:
        from agent.dovie_diagnostics import emit_dovie_diagnostic

        emit_dovie_diagnostic("[dovie-team-mirror-debug]", {"stage": stage, **fields})
    except Exception:
        pass


def mirror_event_to_conversation(
    db: Any,
    *,
    mission_id: str,
    binding: dict[str, Any] | None = None,
    event: dict[str, Any],
    source: str = "",
    target_session_id: str = "",
) -> dict[str, Any]:
    """Live Team Mission events are no longer mirrored into the main session.

    The Team Mission canvas and approval UI consume the canonical
    ``team_mission_events`` projection. Keeping this function as a no-op lets
    older call sites remain harmless while legacy recovery below can still read
    historical mirror rows.
    """
    frame = dict(event or {})
    _emit_mirror_diagnostic(
        "skip-live-mirror-disabled",
        mission_id=mission_id,
        event_type=text(frame.get("type")),
        source=source,
        run_id=text(frame.get("run_id")),
        session_id=text(frame.get("conversation_session_id")),
        target_session_id=text(target_session_id),
        has_binding=isinstance(binding, dict) and bool(binding),
    )
    return {}
