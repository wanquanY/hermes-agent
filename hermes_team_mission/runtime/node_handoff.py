from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.9
    return max(0.0, min(parsed, 1.0))


def _task_id_from_metadata(metadata: Mapping[str, Any]) -> str:
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), Mapping) else {}
    return _text(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
    )


def _task_id_for_node(
    mission: Mapping[str, Any],
    node: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> str:
    return (
        _task_id_from_metadata(_metadata(node.get("metadata")))
        or _task_id_from_metadata(_metadata(binding.get("metadata")))
        or _task_id_from_metadata(_metadata(mission.get("metadata")))
        or _text(mission.get("mission_id"))
    )


def _append_handoff_event(
    db: Any,
    *,
    mission_id: str,
    run_id: str,
    event: dict[str, Any],
    event_errors: list[Exception],
) -> None:
    append = getattr(db, "append_team_mission_run_event", None)
    if not callable(append):
        return
    try:
        append(mission_id=mission_id, run_id=run_id, event=event)
    except Exception as exc:  # pragma: no cover - tool-level recovery owns this path
        event_errors.append(exc)


def record_team_mission_node_handoff(
    *,
    db: Any,
    mission: Mapping[str, Any],
    node: Mapping[str, Any],
    binding: Mapping[str, Any],
    status: str,
    result: str = "",
    summary: str = "",
    payload: Mapping[str, Any] | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    next_context: Mapping[str, Any] | None = None,
    output_contract: Mapping[str, Any] | None = None,
    source: str = "authoritative",
    confidence: Any = 0.9,
    visibility: str = "handoff",
    emit_event: bool = True,
) -> dict[str, Any]:
    """Persist a node handoff without crossing the runtime terminal boundary.

    The handoff tool executes inside the node's model turn.  Its return value is
    followed by the node's user-visible final response, so recording the
    deliverable cannot also finish the node.  ``message.complete``/``error`` is
    the sole terminal barrier and is reduced by ``TeamMissionEventMixin``.
    """

    mission_id = _text(binding.get("mission_id") or mission.get("mission_id"))
    node_id = _text(binding.get("node_id") or node.get("node_id") or node.get("id"))
    run_id = _text(binding.get("run_id"))
    if not mission_id or not node_id or not run_id:
        return {}

    normalized_status = _text(status) or "completed"
    normalized_result = _text(result)
    normalized_summary = _text(summary)
    output_contract = _metadata(output_contract or node.get("output_contract"))
    task_id = _task_id_for_node(mission, node, binding)
    deliverable = db.upsert_team_mission_deliverable(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        task_id=task_id,
        status=normalized_status,
        result=normalized_result,
        summary=normalized_summary,
        payload=_metadata(payload),
        artifact_refs=_records(artifact_refs),
        next_context=_metadata(next_context),
        output_contract=output_contract,
        source=_text(source) or "authoritative",
        confidence=_confidence(confidence),
        visibility=_text(visibility) or "handoff",
    )
    if not deliverable:
        return {}

    current_node = db.get_team_mission_node(mission_id, node_id) or dict(node)
    event_errors: list[Exception] = []
    if emit_event:
        event_payload = {
            "mission_id": mission_id,
            "missionId": mission_id,
            "node_id": node_id,
            "nodeId": node_id,
            "run_id": run_id,
            "runId": run_id,
            "task_id": task_id,
            "taskId": task_id,
            "deliverable_id": deliverable.get("deliverable_id") or "",
            "deliverableId": deliverable.get("deliverable_id") or "",
            "status": deliverable.get("status") or normalized_status,
            "node_status": _text(current_node.get("status")) or "running",
            "nodeStatus": _text(current_node.get("status")) or "running",
            "result": deliverable.get("result") or normalized_result,
            "summary": deliverable.get("summary") or normalized_summary,
            "artifact_refs": deliverable.get("artifact_refs") or [],
            "artifactRefs": deliverable.get("artifact_refs") or [],
            "source": deliverable.get("source") or "authoritative",
            "visibility": deliverable.get("visibility") or visibility or "handoff",
            "channel": "handoff",
        }
        _append_handoff_event(
            db,
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.node.deliverable.recorded", "payload": event_payload},
            event_errors=event_errors,
        )

    return {
        "mission_id": mission_id,
        "node_id": node_id,
        "run_id": run_id,
        "task_id": task_id,
        "status": _text(current_node.get("status")) or "running",
        "node": current_node,
        "deliverable": deliverable,
        "event_errors": event_errors,
    }
