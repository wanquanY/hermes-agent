from __future__ import annotations

import time
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


def _node_terminal_status(status: str, result: str = "") -> str:
    normalized = _text(status).lower()
    normalized_result = _text(result).lower()
    if normalized in {"verified"}:
        return "verified"
    if normalized in {"completed", "complete", "success", "succeeded", "passed", "pass"}:
        return "completed"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"interrupted"}:
        return "interrupted"
    if normalized in {"failed", "failure", "error"}:
        return "failed"
    if normalized in {"blocked", "partial", "partially_completed", "needs_input", "needs-input"}:
        return "blocked"
    if normalized_result in {"fail", "failed", "error"}:
        return "failed"
    return "completed"


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


def _task_id_for_node(mission: Mapping[str, Any], node: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    return (
        _task_id_from_metadata(_metadata(node.get("metadata")))
        or _task_id_from_metadata(_metadata(binding.get("metadata")))
        or _task_id_from_metadata(_metadata(mission.get("metadata")))
        or _text(mission.get("mission_id"))
    )


def _append_finish_event(
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
    except Exception as exc:  # pragma: no cover - exercised through tool-level failure handling
        event_errors.append(exc)


def finish_team_mission_node_run(
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
    terminal_seq: int = 0,
    emit_events: bool = True,
) -> dict[str, Any]:
    """Persist a Team Mission node's authoritative handoff and terminal state.

    The deliverable tool is the owner of successful node completion. Runtime
    ``message.complete`` remains a protocol backstop for nodes that terminate
    without submitting this hidden handoff.
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

    finished_at = time.time()
    node_status = _node_terminal_status(
        _text(deliverable.get("status")) or normalized_status,
        _text(deliverable.get("result")) or normalized_result,
    )
    metadata = _metadata(node.get("metadata"))
    metadata.update({
        "last_run_id": run_id,
        "last_run_terminal_event": "mission.node.finished",
        "last_run_terminal_status": node_status,
        "last_run_terminal_seq": int(terminal_seq or 0),
        "last_run_finished_at": finished_at,
        "last_run_finish_source": "team_mission_submit_deliverable",
        "last_deliverable_id": _text(deliverable.get("deliverable_id")),
        "last_deliverable_status": _text(deliverable.get("status")),
        "last_deliverable_result": _text(deliverable.get("result")),
        "last_deliverable_source": _text(deliverable.get("source")) or "authoritative",
        "last_deliverable_run_id": run_id,
    })
    updated_node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=_text(node.get("kind") or "worker"),
        title=_text(node.get("title")),
        objective=_text(node.get("objective")),
        status=node_status,
        assignee_profile_id=_text(node.get("assignee_profile_id")),
        assignee_profile_version_id=_text(node.get("assignee_profile_version_id")),
        runtime_scope_key=_text(node.get("runtime_scope_key") or binding.get("runtime_scope_key")),
        output_contract=output_contract,
        metadata=metadata,
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )

    event_errors: list[Exception] = []
    if emit_events:
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
            "node_status": node_status,
            "nodeStatus": node_status,
            "result": deliverable.get("result") or normalized_result,
            "summary": deliverable.get("summary") or normalized_summary,
            "artifact_refs": deliverable.get("artifact_refs") or [],
            "artifactRefs": deliverable.get("artifact_refs") or [],
            "source": deliverable.get("source") or "authoritative",
            "visibility": deliverable.get("visibility") or visibility or "handoff",
            "channel": "handoff",
        }
        _append_finish_event(
            db,
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.node.deliverable.recorded", "payload": event_payload},
            event_errors=event_errors,
        )
        _append_finish_event(
            db,
            mission_id=mission_id,
            run_id=run_id,
            event={
                "type": "mission.node.finished",
                "payload": {
                    **event_payload,
                    "node": updated_node,
                    "finished_at": finished_at,
                    "finishedAt": finished_at,
                },
            },
            event_errors=event_errors,
        )

    reduced = {}
    reducer = getattr(db, "reduce_team_mission_graph", None)
    if callable(reducer):
        reduced = reducer(mission_id) or {}
    try:
        from hermes_team_mission.runtime.snapshot_events import append_team_mission_snapshot_updated

        append_team_mission_snapshot_updated(
            db,
            mission_id=mission_id,
            reason="node_finished",
            node_id=node_id,
            run_id=run_id,
            status=node_status,
        )
    except Exception:
        pass

    compiler = getattr(db, "compile_team_mission_memory", None)
    if callable(compiler):
        compile_mode = "final"
        if node_status in {"cancelled", "interrupted"}:
            compile_mode = "canceled"
        elif node_status in {"failed", "blocked"}:
            compile_mode = "blocked"
        try:
            compiler(mission_id=mission_id, mode=compile_mode, source_run_ids=[run_id], emit_event=True)
        except Exception:
            pass

    return {
        "mission_id": mission_id,
        "node_id": node_id,
        "run_id": run_id,
        "task_id": task_id,
        "status": node_status,
        "node": updated_node,
        "deliverable": deliverable,
        "event_errors": event_errors,
        "reduced": reduced,
    }
