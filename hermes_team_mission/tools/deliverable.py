"""Internal Team Mission deliverable submission tools.

The tool in this module separates machine-readable node handoff from the
assistant text stream. Workers submit structured payloads here; user-visible
messages remain ordinary natural-language runtime output.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from hermes_team_mission.context.worker_context import TOOL_ARGS_BUDGET_CHARS
from hermes_team_mission.runtime.node_handoff import record_team_mission_node_handoff
from tools.registry import registry, tool_error, tool_result
from hermes_team_mission.tools.planning import TOOL_RESULT_BUDGET_CHARS
from hermes_team_mission.tools.planning import _active_run_id
from hermes_team_mission.tools.planning import _get_db


_TOOLSET = "team_mission_handoff"
_MAX_DELIVERABLE_PAYLOAD_CHARS = 16 * 1024
logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return len(str(value or ""))


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


def _task_id_for_node(mission: Mapping[str, Any], node: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    node_metadata = _metadata(node.get("metadata"))
    binding_metadata = _metadata(binding.get("metadata"))
    mission_metadata = _metadata(mission.get("metadata"))
    return (
        _task_id_from_metadata(node_metadata)
        or _task_id_from_metadata(binding_metadata)
        or _task_id_from_metadata(mission_metadata)
        or _text(mission.get("mission_id"))
    )


def _node_by_id(graph: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    for node in graph.get("nodes", []) if isinstance(graph.get("nodes"), list) else []:
        if isinstance(node, Mapping) and _text(node.get("node_id") or node.get("id")) == node_id:
            return dict(node)
    return {}


def _run_context(args: Mapping[str, Any], parent_agent=None) -> tuple[Any, str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | str:
    db = _get_db(parent_agent)
    if db is None:
        return "Session database is not available."
    run_id = _active_run_id(dict(args), parent_agent)
    if not run_id:
        return "run_id is required for Team Mission deliverable submission."
    binding = db.get_team_mission_run_binding(run_id)
    if not binding:
        return "Current run is not bound to a Team Mission node."
    mission_id = _text(binding.get("mission_id"))
    node_id = _text(binding.get("node_id"))
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return "Bound Team Mission was not found."
    node = _node_by_id(graph, node_id) or db.get_team_mission_node(mission_id, node_id)
    if not node:
        return "Bound Team Mission node was not found."
    return db, run_id, binding, mission, graph, node


def _normalize_payload(args: Mapping[str, Any], *, node_id: str, status: str, result: str, summary: str) -> dict[str, Any]:
    payload = _metadata(args.get("deliverable") or args.get("payload"))
    if not payload:
        payload = {
            key: value
            for key, value in {
                "node_id": node_id,
                "status": status,
                "result": result,
                "summary": summary,
                "deliverables": args.get("deliverables"),
                "verification": args.get("verification"),
                "risks": args.get("risks"),
            }.items()
            if value not in (None, "", [], {})
        }
    payload.setdefault("node_id", node_id)
    payload.setdefault("status", status)
    if result:
        payload.setdefault("result", result)
    if summary:
        payload.setdefault("summary", summary)
    return payload


def _mark_event_emit_failed(
    *,
    db: Any,
    mission_id: str,
    node: Mapping[str, Any],
    deliverable: Mapping[str, Any],
    error: Exception,
) -> None:
    upsert = getattr(db, "upsert_team_mission_node", None)
    if not callable(upsert):
        return
    mission_id = _text(mission_id)
    node_id = _text(node.get("node_id") or node.get("id"))
    node_getter = getattr(db, "get_team_mission_node", None)
    if callable(node_getter):
        current_node = node_getter(mission_id, node_id) or {}
        if isinstance(current_node, Mapping) and current_node:
            node = current_node
    metadata = _metadata(node.get("metadata"))
    metadata.update({
        "deliverable_event_emit_failed": True,
        "deliverable_event_emit_failed_at": time.time(),
        "deliverable_event_emit_error": _text(error)[:1000],
        "pending_deliverable_event_id": _text(deliverable.get("deliverable_id")),
    })
    upsert(
        mission_id=mission_id,
        node_id=node_id,
        kind=_text(node.get("kind") or "worker"),
        title=_text(node.get("title")),
        objective=_text(node.get("objective")),
        status=_text(node.get("status") or "running"),
        assignee_profile_id=_text(node.get("assignee_profile_id")),
        assignee_profile_version_id=_text(node.get("assignee_profile_version_id")),
        runtime_scope_key=_text(node.get("runtime_scope_key")),
        output_contract=_metadata(node.get("output_contract")),
        metadata=metadata,
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )


def _handle_submit_deliverable(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    if _json_size(args) > TOOL_ARGS_BUDGET_CHARS:
        return tool_error(
            f"team_mission_submit_deliverable arguments are too large. "
            f"Keep the tool arguments under {TOOL_ARGS_BUDGET_CHARS} chars and move large files to artifact paths."
        )
    ctx = _run_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, run_id, binding, mission, _graph, node = ctx
    node_id = _text(binding.get("node_id"))
    if not node_id:
        return tool_error("This run is not bound to a Team Mission node.")
    status = _text(args.get("status")) or _text(args.get("state")) or "completed"
    result = _text(args.get("result") or args.get("outcome"))
    summary = _text(args.get("summary") or args.get("final_summary") or args.get("finalSummary"))
    payload = _normalize_payload(args, node_id=node_id, status=status, result=result, summary=summary)
    if _json_size(payload) > _MAX_DELIVERABLE_PAYLOAD_CHARS:
        return tool_error(
            f"handoff deliverable payload is too large ({_json_size(payload)} chars). "
            f"Keep structured payload under {_MAX_DELIVERABLE_PAYLOAD_CHARS} chars and put large content in files/artifact_refs."
        )
    if not summary:
        summary = _text(payload.get("summary") or payload.get("finalSummary") or payload.get("description"))
    if not summary:
        return tool_error("summary is required. Provide a concise handoff summary for the Team Leader and downstream nodes.")
    artifact_refs = _list_records(
        args.get("artifact_refs")
        or args.get("artifactRefs")
        or args.get("artifacts")
        or payload.get("artifact_refs")
        or payload.get("artifactRefs")
        or payload.get("artifacts")
    )
    next_context = _metadata(args.get("next_context") or args.get("nextContext") or payload.get("next_context") or payload.get("nextContext"))
    output_contract = _metadata(node.get("output_contract"))
    handoff = record_team_mission_node_handoff(
        db=db,
        mission=mission,
        node=node,
        binding=binding,
        status=status,
        result=result,
        summary=summary,
        payload=payload,
        artifact_refs=artifact_refs,
        next_context=next_context,
        output_contract=output_contract,
        source="authoritative",
        confidence=_confidence(args.get("confidence")),
        visibility="handoff",
    )
    deliverable = handoff.get("deliverable") if isinstance(handoff, Mapping) else {}
    if not deliverable:
        return tool_error("Failed to persist Team Mission handoff deliverable.")
    mission_id = _text(binding.get("mission_id"))
    task_id = _text(handoff.get("task_id") if isinstance(handoff, Mapping) else "") or _task_id_for_node(mission, node, binding)
    event_errors = handoff.get("event_errors") if isinstance(handoff, Mapping) else []
    if event_errors:
        exc = event_errors[0]
        logger.error(
            "Failed to emit Team Mission deliverable event mission_id=%s node_id=%s run_id=%s deliverable_id=%s error=%s",
            mission_id,
            node_id,
            run_id,
            deliverable.get("deliverable_id") or "",
            exc,
        )
        _mark_event_emit_failed(
            db=db,
            mission_id=mission_id,
            node=node,
            deliverable=deliverable,
            error=exc,
        )
    return tool_result(
        success=True,
        deliverable_id=deliverable.get("deliverable_id") or "",
        mission_id=_text(binding.get("mission_id")),
        node_id=node_id,
        run_id=run_id,
        task_id=task_id,
        status=deliverable.get("status") or status,
        result=deliverable.get("result") or result,
        summary=deliverable.get("summary") or summary,
        artifact_count=len(deliverable.get("artifact_refs") or []),
        source="authoritative",
        visibility="handoff",
        channel="handoff",
        next="Continue with a concise user-visible natural-language final response. Do not print the structured JSON deliverable.",
    )


def _handle_node_heartbeat(args: dict[str, Any], parent_agent=None, **_kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    ctx = _run_context(args, parent_agent)
    if isinstance(ctx, str):
        return tool_error(ctx)
    db, run_id, binding, _mission, _graph, node = ctx
    mission_id = _text(binding.get("mission_id"))
    node_id = _text(binding.get("node_id"))
    now = time.time()
    note = _text(args.get("note") or args.get("summary") or args.get("status"))
    metadata = _metadata(node.get("metadata"))
    metadata.update({
        "heartbeat_at": now,
        "last_heartbeat_at": now,
        "heartbeat_run_id": run_id,
        "heartbeat_note": note[:1000],
    })
    db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=_text(node.get("kind") or "worker"),
        title=_text(node.get("title")),
        objective=_text(node.get("objective")),
        status=_text(node.get("status") or "running"),
        assignee_profile_id=_text(node.get("assignee_profile_id")),
        assignee_profile_version_id=_text(node.get("assignee_profile_version_id")),
        runtime_scope_key=_text(node.get("runtime_scope_key")),
        output_contract=_metadata(node.get("output_contract")),
        metadata=metadata,
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )
    try:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={
                "type": "mission.node.heartbeat",
                "payload": {
                    "mission_id": mission_id,
                    "missionId": mission_id,
                    "node_id": node_id,
                    "nodeId": node_id,
                    "run_id": run_id,
                    "runId": run_id,
                    "heartbeat_at": now,
                    "heartbeatAt": now,
                    "note": note,
                    "visibility": "internal",
                    "channel": "system",
                },
            },
        )
    except Exception:
        logger.exception(
            "Failed to emit Team Mission heartbeat event mission_id=%s node_id=%s run_id=%s",
            mission_id,
            node_id,
            run_id,
        )
    return tool_result(
        success=True,
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        heartbeat_at=now,
        note=note,
        visibility="internal",
        channel="system",
    )


registry.register(
    name="team_mission_submit_deliverable",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_submit_deliverable",
        "description": (
            "Submit the current Team Mission node's hidden machine-readable handoff deliverable. "
            "Use this after completing the assigned node work and before the final visible response. "
            "Do not print this structured payload in the assistant message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["completed", "blocked", "failed", "partial"],
                    "description": "Node handoff status.",
                },
                "result": {
                    "type": "string",
                    "maxLength": 80,
                    "description": "Short outcome label such as PASS, FAIL, BLOCKED, or PARTIAL.",
                },
                "summary": {
                    "type": "string",
                    "maxLength": 1600,
                    "description": "Concise handoff summary for the Team Leader and downstream nodes.",
                },
                "deliverable": {
                    "type": "object",
                    "description": "Structured machine-readable payload matching the node output_contract. Keep it bounded; put long content in files.",
                },
                "artifact_refs": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "object"},
                    "description": "Produced files or artifact references.",
                },
                "next_context": {
                    "type": "object",
                    "description": "Optional structured context for downstream nodes, such as constraints, open questions, or handoff target.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence from 0 to 1. Defaults to 0.9.",
                },
            },
            "required": ["status", "summary", "deliverable"],
        },
    },
    handler=_handle_submit_deliverable,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)


registry.register(
    name="team_mission_node_heartbeat",
    toolset=_TOOLSET,
    schema={
        "name": "team_mission_node_heartbeat",
        "description": (
            "Record progress heartbeat for the current Team Mission node. "
            "Use during long-running node work so Hermes does not reclaim the node as stale."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": "Short progress note for internal runtime diagnostics.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Optional active run id; normally inferred from the current bound run.",
                },
            },
            "additionalProperties": False,
        },
    },
    handler=_handle_node_heartbeat,
    emoji="",
    max_result_size_chars=TOOL_RESULT_BUDGET_CHARS,
)


__all__ = [name for name in globals() if not name.startswith("__")]
