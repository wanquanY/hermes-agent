from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hermes_team_mission.context.artifact_refs import dedupe_artifact_refs
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind
from hermes_team_mission.domain.statuses import is_terminal_mission_status


USER_VISIBLE_ARTIFACT_VISIBILITIES = {"", "user", "report", "public"}
IGNORED_RESULT_NODE_KINDS = {"root", "approval_gate"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _artifact_is_user_visible(ref: Mapping[str, Any]) -> bool:
    visibility = _text(ref.get("visibility") or ref.get("artifact_visibility") or ref.get("artifactVisibility")).lower()
    return visibility in USER_VISIBLE_ARTIFACT_VISIBILITIES


def _latest_deliverable_by_node(deliverables: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for deliverable in deliverables:
        node_id = _text(deliverable.get("node_id") or deliverable.get("nodeId"))
        if not node_id:
            continue
        current = latest.get(node_id)
        candidate_key = (
            float(deliverable.get("updated_at") or deliverable.get("updatedAt") or 0),
            float(deliverable.get("created_at") or deliverable.get("createdAt") or 0),
            _text(deliverable.get("deliverable_id") or deliverable.get("deliverableId")),
        )
        current_key = (
            float((current or {}).get("updated_at") or (current or {}).get("updatedAt") or 0),
            float((current or {}).get("created_at") or (current or {}).get("createdAt") or 0),
            _text((current or {}).get("deliverable_id") or (current or {}).get("deliverableId")),
        )
        if current is None or candidate_key >= current_key:
            latest[node_id] = deliverable
    return latest


def _node_result(node: Mapping[str, Any], deliverable: Mapping[str, Any] | None) -> dict[str, Any]:
    deliverable = deliverable if isinstance(deliverable, Mapping) else {}
    artifact_refs = deliverable.get("artifact_refs") or deliverable.get("artifactRefs") or []
    artifact_refs = [dict(ref) for ref in artifact_refs if isinstance(ref, Mapping)]
    return {
        "node_id": _text(node.get("node_id") or node.get("id")),
        "nodeId": _text(node.get("node_id") or node.get("id")),
        "kind": normalize_team_mission_node_kind(node.get("kind")),
        "title": _text(node.get("title")),
        "status": _text(node.get("status")),
        "deliverable_id": _text(deliverable.get("deliverable_id") or deliverable.get("deliverableId")),
        "deliverableId": _text(deliverable.get("deliverable_id") or deliverable.get("deliverableId")),
        "result": _text(deliverable.get("result")),
        "summary": _text(deliverable.get("summary")),
        "artifact_refs": artifact_refs,
        "artifactRefs": artifact_refs,
    }


def _summary_from_node_results(node_results: list[dict[str, Any]], outcome: str) -> str:
    synthesis = next(
        (
            item for item in reversed(node_results)
            if _text(item.get("kind")) == "synthesis" and _text(item.get("summary"))
        ),
        {},
    )
    if synthesis:
        return _text(synthesis.get("summary"))
    summaries = [
        _text(item.get("summary"))
        for item in node_results
        if _text(item.get("summary"))
    ]
    if summaries:
        return "\n".join(f"- {summary}" for summary in summaries[:8])
    return f"Mission {outcome}: no presentable output."


def finalize_team_mission_result(db: Any, mission_id: str) -> dict[str, Any]:
    mission_id = _text(mission_id)
    if not mission_id:
        return {}
    graph_getter = getattr(db, "get_team_mission_graph", None)
    if not callable(graph_getter):
        return {}
    graph = graph_getter(mission_id) or {}
    mission = _mapping(graph.get("mission"))
    status = _text(mission.get("status")).lower()
    if not is_terminal_mission_status(status):
        return {}
    deliverables = [
        item for item in (graph.get("deliverables") or [])
        if isinstance(item, dict)
    ]
    if not deliverables:
        lister = getattr(db, "list_team_mission_deliverables", None)
        if callable(lister):
            deliverables = lister(mission_id=mission_id, limit=500)
    deliverable_by_node = _latest_deliverable_by_node(deliverables)
    node_results = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        kind = normalize_team_mission_node_kind(node.get("kind"))
        if kind in IGNORED_RESULT_NODE_KINDS:
            continue
        node_id = _text(node.get("node_id") or node.get("id"))
        node_results.append(_node_result(node, deliverable_by_node.get(node_id)))
    artifact_refs = []
    for item in node_results:
        for ref in item.get("artifact_refs") or []:
            if isinstance(ref, Mapping) and _artifact_is_user_visible(ref):
                artifact_refs.append(dict(ref))
    artifact_refs = dedupe_artifact_refs(artifact_refs)
    summary_text = _summary_from_node_results(node_results, status)
    metadata = {
        "source": "team_mission.result.finalize",
        "conversation_id": _text(mission.get("conversation_id")),
        "team_id": _text(mission.get("team_id")),
        "mode": _text(mission.get("mode")),
    }
    upsert = getattr(db, "upsert_team_mission_result", None)
    if not callable(upsert):
        return {}
    result = upsert(
        mission_id=mission_id,
        activity_id=f"mission:{mission_id}",
        status=status,
        outcome=status,
        summary_text=summary_text,
        node_results=node_results,
        artifact_refs=artifact_refs,
        metadata=metadata,
    )
    source_event = {
        "type": "mission.result.recorded",
        "payload": {
            "mission_id": mission_id,
            "missionId": mission_id,
            "result_id": result.get("result_id") or "",
            "resultId": result.get("result_id") or "",
            "status": result.get("status") or status,
            "outcome": result.get("outcome") or status,
            "summary_text": result.get("summary_text") or "",
            "summaryText": result.get("summary_text") or "",
            "artifact_refs": result.get("artifact_refs") or [],
            "artifactRefs": result.get("artifact_refs") or [],
        },
    }
    dedupe_key = f"mission-result-recorded:{mission_id}:{result.get('result_id') or ''}"
    append_projection = getattr(db, "_append_team_mission_state_projection_event", None)
    if callable(append_projection) and result:
        try:
            append_projection(
                mission_id=mission_id,
                event=source_event,
                dedupe_key=dedupe_key,
            )
            return result
        except Exception:
            pass
    append = getattr(db, "append_team_mission_structural_event", None)
    if callable(append) and result:
        try:
            append(
                mission_id=mission_id,
                source_event=source_event,
                dedupe_key=dedupe_key,
            )
        except Exception:
            pass
    return result
