from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List

from hermes_team_mission.domain.utils import MEMORY_COMMITTED_STATUS
from hermes_team_mission.domain.utils import MEMORY_TERMINAL_STATUSES
from hermes_team_mission.domain.utils import MEMORY_VISIBLE_TO_WORKER
from hermes_team_mission.domain.utils import dedupe_text
from hermes_team_mission.domain.utils import event_artifacts
from hermes_team_mission.domain.utils import event_text
from hermes_team_mission.domain.utils import stable_id
from hermes_team_mission.domain.utils import stringify_content
from hermes_team_mission.domain.utils import text
from hermes_team_mission.domain.utils import tokenize


_DEPENDENCY_EDGE_KINDS = {"depends_on", "dependency", "blocks", "delegates"}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _task_id_from_metadata(metadata: Dict[str, Any] | None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), dict) else {}
    return text(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
    )


def _task_id_from_node_and_binding(node: Dict[str, Any] | None, binding: Dict[str, Any] | None = None) -> str:
    node_metadata = node.get("metadata") if isinstance(node, dict) and isinstance(node.get("metadata"), dict) else {}
    binding_metadata = binding.get("metadata") if isinstance(binding, dict) and isinstance(binding.get("metadata"), dict) else {}
    return _task_id_from_metadata(node_metadata) or _task_id_from_metadata(binding_metadata)


def _conversation_item_as_team_memory(item: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(item, dict) or not item:
        return {}
    payload = item.get("structured_payload") if isinstance(item.get("structured_payload"), dict) else {}
    legacy = payload.get("team_mission") if isinstance(payload.get("team_mission"), dict) else {}
    provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
    visibility = item.get("visibility") if isinstance(item.get("visibility"), dict) else {}
    return {
        "id": text(item.get("memory_id")),
        "memory_id": text(item.get("memory_id")),
        "team_id": text(legacy.get("team_id")),
        "mission_id": text(legacy.get("mission_id") or item.get("activity_id")).removeprefix("mission:"),
        "conversation_session_id": text(item.get("conversation_session_id")),
        "task_id": text(legacy.get("task_id")),
        "scope": text(legacy.get("scope")) or text(item.get("owner_kind")),
        "kind": text(item.get("kind")),
        "content": text(item.get("content")),
        "structured_payload": payload.get("payload") if isinstance(payload.get("payload"), dict) else payload,
        "source_node_ids": list(provenance.get("source_node_ids") or []),
        "source_run_ids": list(provenance.get("source_run_ids") or []),
        "artifact_refs": list(payload.get("artifact_refs") or []),
        "workspace_refs": list(payload.get("workspace_refs") or []),
        "confidence": float(item.get("confidence") or 0),
        "visibility": text(
            legacy.get("visibility")
            or ("team" if visibility.get("kind") in {"conversation", "activity"} else visibility.get("kind"))
        ),
        "status": text(item.get("status")),
        "created_at": float(item.get("created_at") or 0),
        "updated_at": float(item.get("updated_at") or 0),
        "invalidated_at": item.get("invalidated_at"),
        "revision": int(item.get("revision") or 0),
    }


def team_mission_memory_context(db: Any, mission: Dict[str, Any]) -> Dict[str, Any]:
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    conversation_session_id = text(
        metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("conversation_team_session_id")
        or metadata.get("conversationTeamSessionId")
        or metadata.get("team_session_id")
        or metadata.get("teamSessionId")
        or mission.get("leader_session_id")
    )
    active_task = metadata.get("active_task") if isinstance(metadata.get("active_task"), dict) else {}
    task_id = text(
        metadata.get("active_task_id")
        or metadata.get("activeTaskId")
        or active_task.get("task_id")
        or active_task.get("taskId")
        or metadata.get("task_id")
        or metadata.get("taskId")
        or mission.get("mission_id")
    )
    workspace_refs = []
    workspace_id = text(mission.get("workspace_id"))
    workspace_path = text(mission.get("workspace_path"))
    if workspace_id or workspace_path:
        workspace_refs.append({
            "workspace_id": workspace_id,
            "workspace_path": workspace_path,
        })
    return {
        "team_id": text(mission.get("team_id")),
        "mission_id": text(mission.get("mission_id")),
        "conversation_session_id": conversation_session_id,
        "task_id": task_id,
        "workspace_refs": workspace_refs,
    }


def upsert_team_mission_memory_item(
    db: Any,
    *,
    memory_id: str = "",
    team_id: str,
    mission_id: str,
    conversation_session_id: str,
    task_id: str = "",
    scope: str = "mission_task",
    kind: str = "summary",
    content: str,
    structured_payload: Dict[str, Any] | None = None,
    source_node_ids: List[str] | None = None,
    source_run_ids: List[str] | None = None,
    artifact_refs: List[Dict[str, Any]] | None = None,
    workspace_refs: List[Dict[str, Any]] | None = None,
    confidence: float = 0.75,
    visibility: str = "team",
    status: str = MEMORY_COMMITTED_STATUS,
    created_at: float | None = None,
    updated_at: float | None = None,
    invalidated_at: float | None = None,
) -> Dict[str, Any]:
    content = stringify_content(content, limit=4000)
    mission_id = text(mission_id)
    conversation_session_id = text(conversation_session_id)
    if not mission_id or not conversation_session_id or not content:
        return {}
    source_nodes = dedupe_text(source_node_ids or [])
    source_runs = dedupe_text(source_run_ids or [])
    artifacts = [
        dict(item)
        for item in (artifact_refs or [])
        if isinstance(item, dict) and text(item.get("uri") or item.get("path") or item.get("id"))
    ]
    if status == MEMORY_COMMITTED_STATUS and not (source_nodes or source_runs or artifacts):
        return {}
    normalized_id = text(memory_id) or stable_id(
        "tmmem",
        mission_id,
        task_id,
        scope,
        kind,
        content,
        ",".join(source_nodes),
        ",".join(source_runs),
    )
    memory = db.conversation_memory
    normalized_scope = text(scope) or "mission_task"
    if normalized_scope in {"conversation", "team"}:
        owner_kind, owner_id, activity_id, node_id = (
            "conversation", conversation_session_id, "", ""
        )
        visibility_policy = {"kind": "conversation"}
    elif normalized_scope == "node" and source_nodes:
        owner_kind, owner_id, activity_id, node_id = (
            "node", source_nodes[0], f"mission:{mission_id}", source_nodes[0]
        )
        visibility_policy = {
            "kind": "node", "activity_id": activity_id, "node_id": node_id
        }
    else:
        owner_kind, owner_id, activity_id, node_id = (
            "activity", f"mission:{mission_id}", f"mission:{mission_id}", ""
        )
        visibility_policy = {"kind": "activity", "activity_id": activity_id}
    normalized_status = text(status) or MEMORY_COMMITTED_STATUS
    if normalized_status == "deleted":
        normalized_status = "invalidated"
    payload = {
        "payload": structured_payload or {},
        "artifact_refs": artifacts,
        "workspace_refs": list(workspace_refs or []),
        "team_mission": {
            "team_id": text(team_id),
            "mission_id": mission_id,
            "task_id": text(task_id),
            "scope": normalized_scope,
            "visibility": text(visibility) or "team",
        },
    }
    existing = memory.get_item(normalized_id)
    if existing:
        result = memory.update_item(
            normalized_id,
            expected_revision=int(existing.get("revision") or 0),
            content=content,
            structured_payload=payload,
            visibility=visibility_policy,
            confidence=max(0.0, min(float(confidence or 0), 1.0)),
            status=normalized_status,
        )
    else:
        result = memory.create_item(
            memory_id=normalized_id,
            conversation_session_id=conversation_session_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            activity_id=activity_id,
            node_id=node_id,
            kind=text(kind) if text(kind) in {
                "fact", "preference", "decision", "commitment", "constraint",
                "risk", "open_question", "artifact", "summary",
            } else "fact",
            content=content,
            structured_payload=payload,
            visibility=visibility_policy,
            provenance={
                "source_node_ids": source_nodes,
                "source_run_ids": source_runs,
            },
            confidence=max(0.0, min(float(confidence or 0), 1.0)),
            status=normalized_status,
            valid_from=created_at,
        )
    return _conversation_item_as_team_memory(result)


def list_team_mission_memory_items(
    db: Any,
    *,
    mission_id: str = "",
    conversation_session_id: str = "",
    team_id: str = "",
    task_id: str = "",
    kinds: List[str] | None = None,
    statuses: List[str] | None = None,
    visibility: List[str] | None = None,
    include_deleted: bool = False,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    normalized_statuses = dedupe_text(statuses or [])
    if not normalized_statuses and not include_deleted:
        normalized_statuses = ["proposed", "committed", "superseded"]
    resolved_conversation = text(conversation_session_id)
    if text(mission_id) and not resolved_conversation:
        graph = db.team_mission_graphs.get_team_mission_graph(text(mission_id))
        mission = graph.get("mission") if isinstance(graph, dict) else {}
        if isinstance(mission, dict):
            resolved_conversation = team_mission_memory_context(db, mission)[
                "conversation_session_id"
            ]
    rows = db.conversation_memory.list_items(
        conversation_session_id=resolved_conversation,
        kinds=dedupe_text(kinds or []),
        statuses=normalized_statuses,
        limit=limit,
    )
    items = [_conversation_item_as_team_memory(row) for row in rows]
    return [
        item for item in items
        if item
        and (not text(mission_id) or item.get("mission_id") == text(mission_id))
        and (not text(team_id) or item.get("team_id") == text(team_id))
        and (not text(task_id) or item.get("task_id") == text(task_id))
        and (not visibility or item.get("visibility") in dedupe_text(visibility))
    ]


def update_team_mission_memory_item(
    db: Any,
    memory_id: str,
    *,
    content: str | None = None,
    structured_payload: Dict[str, Any] | None = None,
    visibility: str | None = None,
    status: str | None = None,
    confidence: float | None = None,
) -> Dict[str, Any]:
    memory_id = text(memory_id)
    if not memory_id:
        return {}
    existing_raw = db.conversation_memory.get_item(memory_id)
    existing = _conversation_item_as_team_memory(existing_raw)
    if not existing:
        return {}
    next_status = text(status) or existing["status"]
    invalidated_at = existing.get("invalidated_at")
    if next_status in {"invalidated", "deleted"} and not invalidated_at:
        invalidated_at = time.time()
    next_visibility = existing_raw.get("visibility") or {}
    if visibility is not None:
        next_visibility = {"kind": "conversation"} if visibility == "team" else next_visibility
    updated = db.conversation_memory.update_item(
        memory_id,
        expected_revision=int(existing_raw.get("revision") or 0),
        content=content,
        structured_payload=(
            structured_payload if structured_payload is not None
            else existing_raw.get("structured_payload") or {}
        ),
        visibility=next_visibility,
        status="invalidated" if next_status == "deleted" else next_status,
        confidence=confidence,
    )
    return _conversation_item_as_team_memory(updated)


def delete_team_mission_memory_item(db: Any, memory_id: str) -> Dict[str, Any]:
    return update_team_mission_memory_item(db, memory_id, status="deleted")


def upsert_team_mission_memory_edge(
    db: Any,
    *,
    from_memory_id: str,
    to_memory_id: str = "",
    relation: str,
    metadata: Dict[str, Any] | None = None,
    edge_id: str = "",
    created_at: float | None = None,
) -> Dict[str, Any]:
    from_memory_id = text(from_memory_id)
    relation = text(relation)
    if not from_memory_id or not relation:
        return {}
    normalized_edge_id = text(edge_id) or stable_id(
        "tmmemedge",
        from_memory_id,
        to_memory_id,
        relation,
        _json_dumps(metadata or {}),
    )
    created = float(created_at or time.time())

    edge = db.conversation_memory.upsert_edge(
        edge_id=normalized_edge_id,
        from_memory_id=from_memory_id,
        to_memory_id=text(to_memory_id),
        relation=relation,
        metadata=metadata or {},
    )
    return {
        "id": edge.get("edge_id"),
        "from_memory_id": edge.get("from_memory_id"),
        "to_memory_id": edge.get("to_memory_id") or "",
        "relation": edge.get("relation"),
        "metadata": edge.get("metadata") or {},
        "created_at": edge.get("created_at") or created,
    }


def list_team_mission_memory_edges(
    db: Any,
    *,
    from_memory_id: str = "",
    to_memory_id: str = "",
    relation: str = "",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    return [
        {
            "id": edge.get("edge_id"),
            "from_memory_id": edge.get("from_memory_id"),
            "to_memory_id": edge.get("to_memory_id") or "",
            "relation": edge.get("relation"),
            "metadata": edge.get("metadata") or {},
            "created_at": edge.get("created_at") or 0,
        }
        for edge in db.conversation_memory.list_edges(
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            relation=relation,
            limit=limit,
        )
    ]


def team_mission_binding_events(db: Any, binding: Dict[str, Any], *, limit: int = 2000) -> List[Dict[str, Any]]:
    events = db.runs.list_events(
        text(binding.get("session_id")),
        runtime_scope_key=text(binding.get("runtime_scope_key")),
        limit=limit,
    )
    run_id = text(binding.get("run_id"))
    return [
        event for event in events
        if isinstance(event, dict) and text(event.get("run_id")) == run_id
    ]


def team_mission_bindings_events_map(db: Any, bindings: List[Dict[str, Any]], *, limit_per_run: int = 2000) -> Dict[str, List[Dict[str, Any]]]:
    run_ids = dedupe_text([binding.get("run_id") for binding in bindings if isinstance(binding, dict)])
    if not run_ids:
        return {}
    return db.runs.list_events_by_run_ids(
        run_ids,
        limit_per_run=limit_per_run,
    )


def team_mission_binding_message_excerpt(db: Any, binding: Dict[str, Any]) -> str:
    try:
        messages = db.messages.list(text(binding.get("session_id")))
    except Exception:
        messages = []
    parts: list[str] = []
    for message in reversed(messages[-12:]):
        if not isinstance(message, dict):
            continue
        role = text(message.get("role"))
        if role not in {"assistant", "tool"}:
            continue
        message_text = stringify_content(message.get("content"), limit=1200)
        if message_text:
            parts.append(message_text)
        if len(parts) >= 2:
            break
    return " ".join(reversed(parts))


def compile_memory_for_binding(
    db: Any,
    *,
    mission: Dict[str, Any],
    node: Dict[str, Any],
    binding: Dict[str, Any],
    context: Dict[str, Any],
    task_id: str,
    mode: str,
    preloaded_events: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    run_id = text(binding.get("run_id"))
    node_id = text(node.get("node_id") or binding.get("node_id"))
    if not run_id or not node_id:
        return []
    deliverable_getter = getattr(db, "latest_team_mission_deliverable_for_run", None)
    deliverable = deliverable_getter(run_id) if callable(deliverable_getter) else {}
    deliverable = deliverable if isinstance(deliverable, dict) else {}
    events = [] if deliverable else (preloaded_events if preloaded_events is not None else team_mission_binding_events(db, binding))
    text_parts = []
    if deliverable:
        summary_text = text(deliverable.get("summary"))
        if summary_text:
            text_parts.append(summary_text)
    else:
        text_parts = [event_text(event) for event in events]
        text_parts = [part for part in text_parts if part]
        if not text_parts:
            excerpt = team_mission_binding_message_excerpt(db, binding)
            if excerpt:
                text_parts.append(excerpt)
    raw_artifacts: list[dict[str, Any]] = list(deliverable.get("artifact_refs") or deliverable.get("artifactRefs") or [])
    if not raw_artifacts:
        for event in events:
            raw_artifacts.extend(event_artifacts(event))
    artifact_refs: list[dict[str, Any]] = []
    seen_artifacts: set[str] = set()
    for artifact in raw_artifacts:
        uri = text(artifact.get("uri") or artifact.get("path") or artifact.get("id"))
        if uri and uri not in seen_artifacts:
            seen_artifacts.add(uri)
            artifact_refs.append(artifact)
    node_title = text(node.get("title") or node_id)
    node_objective = text(node.get("objective"))
    node_status = text(node.get("status"))
    summary = stringify_content(" ".join(text_parts), limit=2200)
    if summary:
        content = f"{node_title}: {summary}"
    else:
        status_text = node_status or "completed"
        content = f"{node_title}: node reached status '{status_text}' for objective '{node_objective or text(mission.get('objective'))}'."
    structured_payload = {
        "compiler": "hermes_team_mission_memory_v1",
        "mode": text(mode) or "final",
        "node": {
            "node_id": node_id,
            "kind": text(node.get("kind")),
            "title": node_title,
            "objective": node_objective,
            "status": node_status,
            "role": text((node.get("metadata") or {}).get("role") if isinstance(node.get("metadata"), dict) else ""),
        },
        "run_id": run_id,
        "event_count": len(events),
    }
    if deliverable:
        structured_payload["deliverable"] = {
            "deliverable_id": deliverable.get("deliverable_id") or deliverable.get("deliverableId"),
            "status": deliverable.get("status"),
            "result": deliverable.get("result"),
            "summary": deliverable.get("summary"),
            "payload": deliverable.get("payload") if isinstance(deliverable.get("payload"), dict) else {},
            "source": deliverable.get("source"),
            "visibility": deliverable.get("visibility"),
        }
    items: list[dict[str, Any]] = []
    summary_item = upsert_team_mission_memory_item(
        db,
        team_id=context["team_id"],
        mission_id=context["mission_id"],
        conversation_session_id=context["conversation_session_id"],
        task_id=task_id,
        scope="mission_task",
        kind="summary",
        content=content,
        structured_payload=structured_payload,
        source_node_ids=[node_id],
        source_run_ids=[run_id],
        artifact_refs=artifact_refs,
        workspace_refs=list(context.get("workspace_refs") or []),
        confidence=0.82 if summary else 0.65,
        visibility="team",
        status="proposed",
    )
    if summary_item:
        items.append(summary_item)
    node_metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    risk_level = text(node_metadata.get("risk_level")).lower()
    if risk_level in {"high", "critical"}:
        risk_item = upsert_team_mission_memory_item(
            db,
            team_id=context["team_id"],
            mission_id=context["mission_id"],
            conversation_session_id=context["conversation_session_id"],
            task_id=task_id,
            scope="conversation",
            kind="risk",
            content=f"{node_title}: risk_level={risk_level}; future related tasks should surface this risk before execution.",
            structured_payload={**structured_payload, "risk_level": risk_level},
            source_node_ids=[node_id],
            source_run_ids=[run_id],
            artifact_refs=[],
            workspace_refs=list(context.get("workspace_refs") or []),
            confidence=0.9,
            visibility="team",
            status="proposed",
        )
        if risk_item:
            items.append(risk_item)
    if node_status in {"failed", "blocked", "cancelled", "canceled", "interrupted"}:
        open_item = upsert_team_mission_memory_item(
            db,
            team_id=context["team_id"],
            mission_id=context["mission_id"],
            conversation_session_id=context["conversation_session_id"],
            task_id=task_id,
            scope="conversation",
            kind="open_question",
            content=f"{node_title}: previous execution ended as '{node_status}'. Treat the result as unresolved unless a later memory invalidates it.",
            structured_payload={**structured_payload, "terminal_status": node_status},
            source_node_ids=[node_id],
            source_run_ids=[run_id],
            artifact_refs=artifact_refs,
            workspace_refs=list(context.get("workspace_refs") or []),
            confidence=0.78,
            visibility="team",
            status="proposed",
        )
        if open_item:
            items.append(open_item)
    for artifact in artifact_refs:
        uri = text(artifact.get("uri") or artifact.get("path") or artifact.get("id"))
        if not uri:
            continue
        artifact_item = upsert_team_mission_memory_item(
            db,
            team_id=context["team_id"],
            mission_id=context["mission_id"],
            conversation_session_id=context["conversation_session_id"],
            task_id=task_id,
            scope="mission_task",
            kind="artifact",
            content=f"{node_title} produced artifact: {text(artifact.get('title')) or uri}",
            structured_payload={**structured_payload, "artifact": artifact},
            source_node_ids=[node_id],
            source_run_ids=[run_id],
            artifact_refs=[artifact],
            workspace_refs=list(context.get("workspace_refs") or []),
            confidence=0.95,
            visibility="team",
            status="proposed",
        )
        if artifact_item:
            items.append(artifact_item)
    return items


def _memory_conflict_key(item: Dict[str, Any]) -> str:
    payload = item.get("structured_payload") if isinstance(item.get("structured_payload"), dict) else {}
    subject = text(
        payload.get("conflict_key")
        or payload.get("conflictKey")
        or payload.get("subject")
        or payload.get("key")
    )
    return f"{text(item.get('kind'))}:{subject}" if subject else ""


def _resolve_memory_candidates(
    items: List[Dict[str, Any]],
    *,
    limit: int,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Remove exact duplicates and surface contradictory facts explicitly.

    Similar wording is not evidence that two memories are equivalent. Only an
    exact normalized identity is deduplicated; candidates sharing an explicit
    semantic conflict key with different content remain selected and form a
    conflict set for the model and UI.
    """
    kept: list[dict[str, Any]] = []
    exact_identities: set[str] = set()
    by_conflict_key: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        identity = _json_dumps(
            {
                "kind": text(item.get("kind")),
                "content": " ".join(text(item.get("content")).split()).casefold(),
                "structured_payload": item.get("structured_payload") or {},
            }
        )
        if identity in exact_identities:
            continue
        exact_identities.add(identity)
        kept.append(item)
        conflict_key = _memory_conflict_key(item)
        if conflict_key:
            by_conflict_key.setdefault(conflict_key, []).append(item)
    conflicts: list[dict[str, Any]] = []
    for conflict_key, group in sorted(by_conflict_key.items()):
        distinct_contents = {" ".join(text(item.get("content")).split()).casefold() for item in group}
        if len(distinct_contents) <= 1:
            continue
        conflicts.append(
            {
                "conflict_key": conflict_key,
                "memory_item_ids": [text(item.get("id")) for item in group if text(item.get("id"))],
                "candidates": group,
            }
        )
    return kept[: max(1, min(int(limit or 8), 50))], conflicts


def compile_team_mission_memory(
    db: Any,
    *,
    mission_id: str,
    task_id: str = "",
    mode: str = "final",
    source_run_ids: List[str] | None = None,
    emit_event: bool = True,
) -> Dict[str, Any]:
    mission_id = text(mission_id)
    if not mission_id:
        return {}
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return {}
    context = team_mission_memory_context(db, mission)
    normalized_task_id = text(task_id) or context["task_id"] or mission_id
    requested_runs = set(dedupe_text(source_run_ids or []))
    nodes_by_id = {
        text(node.get("node_id")): node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and text(node.get("node_id"))
    }
    items: list[dict[str, Any]] = []
    compiled_run_ids: list[str] = []
    raw_bindings = [binding for binding in graph.get("run_bindings", []) if isinstance(binding, dict)]
    eligible_bindings: list[dict[str, Any]] = []
    for binding in raw_bindings:
        if not isinstance(binding, dict):
            continue
        run_id = text(binding.get("run_id"))
        if requested_runs and run_id not in requested_runs:
            continue
        node = nodes_by_id.get(text(binding.get("node_id"))) or {}
        binding_task_id = _task_id_from_node_and_binding(node, binding) or normalized_task_id
        node_status = text(node.get("status"))
        if not requested_runs and node_status and node_status not in MEMORY_TERMINAL_STATUSES:
            continue
        eligible_bindings.append(binding)
    events_by_run = team_mission_bindings_events_map(db, eligible_bindings)
    for binding in eligible_bindings:
        run_id = text(binding.get("run_id"))
        node = nodes_by_id.get(text(binding.get("node_id"))) or {}
        binding_task_id = _task_id_from_node_and_binding(node, binding) or normalized_task_id
        compiled = compile_memory_for_binding(
            db,
            mission=mission,
            node=node,
            binding=binding,
            context=context,
            task_id=binding_task_id,
            mode=mode,
            preloaded_events=events_by_run.get(run_id),
        )
        if compiled:
            compiled_run_ids.append(run_id)
            items.extend(compiled)
    item_ids = [item["id"] for item in items if item.get("id")]
    if emit_event and item_ids and compiled_run_ids:
        try:
            db.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=compiled_run_ids[0],
                event={
                    "type": "mission.memory.compiled",
                    "payload": {
                        "mission_id": mission_id,
                        "task_id": normalized_task_id,
                        "memory_item_ids": item_ids,
                        "source_run_ids": compiled_run_ids,
                        "mode": text(mode) or "final",
                    },
                },
            )
        except Exception:
            pass
    return {
        "mission_id": mission_id,
        "task_id": normalized_task_id,
        "conversation_session_id": context["conversation_session_id"],
        "memory_item_ids": item_ids,
        "items": items,
        "source_run_ids": compiled_run_ids,
    }


def score_team_mission_memory_item(
    item: Dict[str, Any],
    *,
    objective: str,
    preferred_node_ids: set[str] | None = None,
) -> float:
    kind_weight = {
        "decision": 2.4,
        "constraint": 2.2,
        "risk": 2.0,
        "artifact": 1.9,
        "summary": 1.5,
        "reusable_result": 1.7,
        "open_question": 1.3,
        "preference": 1.2,
    }.get(text(item.get("kind")), 1.0)
    objective_tokens = tokenize(objective)
    item_tokens = tokenize(
        " ".join([
            text(item.get("content")),
            _json_dumps(item.get("structured_payload") or {}),
        ])
    )
    overlap = len(objective_tokens & item_tokens)
    overlap_score = overlap / max(1, len(objective_tokens)) if objective_tokens else 0
    source_nodes = set(dedupe_text(item.get("source_node_ids") or []))
    dependency_boost = 0.0
    if preferred_node_ids and source_nodes & preferred_node_ids:
        dependency_boost = 2.0
    confidence = float(item.get("confidence") or 0)
    recency = min(float(item.get("updated_at") or 0) / 1_000_000_000, 2.0)
    return kind_weight + (overlap_score * 3.0) + dependency_boost + confidence + recency


def select_team_mission_memory_items(
    db: Any,
    *,
    mission: Dict[str, Any],
    objective: str,
    limit: int,
    visibility: List[str],
    include_team_scope: bool = False,
    preferred_node_ids: set[str] | None = None,
) -> List[Dict[str, Any]]:
    selected, _ = resolve_team_mission_memory_items(
        db,
        mission=mission,
        objective=objective,
        limit=limit,
        visibility=visibility,
        include_team_scope=include_team_scope,
        preferred_node_ids=preferred_node_ids,
    )
    return selected


def resolve_team_mission_memory_items(
    db: Any,
    *,
    mission: Dict[str, Any],
    objective: str,
    limit: int,
    visibility: List[str],
    include_team_scope: bool = False,
    preferred_node_ids: set[str] | None = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    context = team_mission_memory_context(db, mission)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    queries = [{"conversation_session_id": context["conversation_session_id"]}]
    if include_team_scope and context["team_id"]:
        queries.append({"team_id": context["team_id"]})
    for query in queries:
        for item in list_team_mission_memory_items(
            db,
            **query,
            statuses=[MEMORY_COMMITTED_STATUS],
            visibility=visibility,
            limit=500,
        ):
            item_id = text(item.get("id"))
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            candidates.append(item)
    ranked = sorted(
        candidates,
        key=lambda item: score_team_mission_memory_item(
            item,
            objective=objective,
            preferred_node_ids=preferred_node_ids,
        ),
        reverse=True,
    )
    return _resolve_memory_candidates(ranked, limit=limit)


def record_team_mission_memory_references(
    db: Any,
    *,
    items: List[Dict[str, Any]],
    target_id: str,
    relation: str,
    metadata: Dict[str, Any],
) -> None:
    for item in items:
        item_id = text(item.get("id"))
        if not item_id:
            continue
        upsert_team_mission_memory_edge(
            db,
            from_memory_id=item_id,
            to_memory_id=target_id,
            relation=relation,
            metadata=metadata,
        )


def build_team_mission_memory_pack(
    db: Any,
    *,
    mission_id: str,
    objective: str = "",
    workspace_id: str = "",
    limit: int = 8,
    include_team_scope: bool = False,
) -> Dict[str, Any]:
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return {}
    context = team_mission_memory_context(db, mission)
    objective_text = text(objective) or text(mission.get("objective") or mission.get("title"))
    items, conflicts = resolve_team_mission_memory_items(
        db,
        mission=mission,
        objective=objective_text,
        limit=limit,
        visibility=["team", "leader_only"],
        include_team_scope=include_team_scope,
    )
    target_id = f"task:{text(mission.get('mission_id'))}:{context['task_id']}"
    record_team_mission_memory_references(
        db,
        items=items,
        target_id=target_id,
        relation="referenced_by_task",
        metadata={
            "mission_id": text(mission.get("mission_id")),
            "task_id": context["task_id"],
            "objective": objective_text,
            "workspace_id": text(workspace_id or mission.get("workspace_id")),
            "reference_kind": "leader_memory_pack",
        },
    )
    artifact_refs: list[dict[str, Any]] = []
    seen_artifacts: set[str] = set()
    for item in items:
        for artifact in item.get("artifact_refs") or []:
            if not isinstance(artifact, dict):
                continue
            uri = text(artifact.get("uri") or artifact.get("path") or artifact.get("id"))
            if uri and uri not in seen_artifacts:
                seen_artifacts.add(uri)
                artifact_refs.append(artifact)
    return {
        "mission_id": text(mission.get("mission_id")),
        "task_id": context["task_id"],
        "conversation_session_id": context["conversation_session_id"],
        "memory_pack": {
            "items": items,
            "item_ids": [item["id"] for item in items if item.get("id")],
            "conflicts": conflicts,
            "artifact_refs": artifact_refs,
        },
    }


def build_team_mission_memory_slice(
    db: Any,
    *,
    mission_id: str,
    node_id: str,
    objective: str = "",
    limit: int = 5,
    include_team_scope: bool = False,
) -> Dict[str, Any]:
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return {}
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return {}
    dependency_node_ids = {
        text(edge.get("from_node_id"))
        for edge in graph.get("edges", [])
        if isinstance(edge, dict)
        and text(edge.get("to_node_id")) == text(node_id)
        and text(edge.get("kind") or "depends_on") in _DEPENDENCY_EDGE_KINDS
    }
    node_metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    role = text(node_metadata.get("role") or node.get("kind"))
    visibility = ["team", "leader_only"] if role in {"leader", "root"} else list(MEMORY_VISIBLE_TO_WORKER)
    objective_text = text(objective) or text(node.get("objective") or node.get("title") or mission.get("objective"))
    items, conflicts = resolve_team_mission_memory_items(
        db,
        mission=mission,
        objective=objective_text,
        limit=limit,
        visibility=visibility,
        include_team_scope=include_team_scope,
        preferred_node_ids=dependency_node_ids,
    )
    context = team_mission_memory_context(db, mission)
    target_id = f"node:{text(mission.get('mission_id'))}:{text(node_id)}:{context['task_id']}"
    record_team_mission_memory_references(
        db,
        items=items,
        target_id=target_id,
        relation="referenced_by_node",
        metadata={
            "mission_id": text(mission.get("mission_id")),
            "task_id": context["task_id"],
            "node_id": text(node_id),
            "objective": objective_text,
            "dependency_node_ids": sorted(dep for dep in dependency_node_ids if dep),
            "conflicts": conflicts,
            "reference_kind": "worker_memory_slice",
        },
    )
    artifact_refs: list[dict[str, Any]] = []
    seen_artifacts: set[str] = set()
    for item in items:
        for artifact in item.get("artifact_refs") or []:
            if not isinstance(artifact, dict):
                continue
            uri = text(artifact.get("uri") or artifact.get("path") or artifact.get("id"))
            if uri and uri not in seen_artifacts:
                seen_artifacts.add(uri)
                artifact_refs.append(artifact)
    return {
        "mission_id": text(mission.get("mission_id")),
        "node_id": text(node_id),
        "task_id": context["task_id"],
        "conversation_session_id": context["conversation_session_id"],
        "memory_slice": {
            "items": items,
            "item_ids": [item["id"] for item in items if item.get("id")],
            "artifact_refs": artifact_refs,
            "dependency_node_ids": sorted(dep for dep in dependency_node_ids if dep),
        },
    }
