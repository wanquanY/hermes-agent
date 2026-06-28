"""Bounded Team Mission graph and worker-context helpers.

Team Mission has two very different consumers:

* Dovie UI needs full graph projections.
* LLM turns need bounded summaries, slices, and worker prompts.

This module owns the LLM-facing boundary so Team Mission tools do not push full
mission graphs, giant briefs, or unbounded memory text back into the model loop.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from hermes_state_run_event_codec import payload_from_run_event_row


TOOL_RESULT_BUDGET_CHARS = 8 * 1024
TOOL_ARGS_BUDGET_CHARS = 8 * 1024
NODE_TITLE_MAX_CHARS = 160
NODE_OBJECTIVE_MAX_CHARS = 1200
NODE_BRIEF_BACKGROUND_MAX_CHARS = 2048
NODE_BRIEF_GOAL_MAX_CHARS = 1200
NODE_BRIEF_ITEM_MAX_CHARS = 600
NODE_BRIEF_LIST_MAX_ITEMS = 8
WORKER_CONTEXT_MAX_CHARS = 40 * 1024
WORKER_CONTEXT_FIELD_MAX_CHARS = 4 * 1024
WORKER_CONTEXT_BODY_MAX_CHARS = 8 * 1024
PRIOR_ATTEMPTS_LIMIT = 5
PRIOR_ATTEMPT_MAX_CHARS = 4 * 1024
RECENT_EVENTS_LIMIT = 20
RECENT_EVENT_MAX_CHARS = 2 * 1024
GRAPH_SUMMARY_NODE_LIMIT = 24
GRAPH_SUMMARY_BUDGET_CHARS = 6 * 1024
GRAPH_SLICE_NODE_LIMIT = 30


def text(value: Any) -> str:
    return str(value or "").strip()


def metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def cap_text(value: Any, limit: int, *, marker: str = " ... [truncated]") -> str:
    value_text = text(value)
    if not value_text or len(value_text) <= limit:
        return value_text
    marker = marker if limit > len(marker) else ""
    return value_text[: max(0, limit - len(marker))].rstrip() + marker


def text_list(value: Any, *, limit: int = NODE_BRIEF_LIST_MAX_ITEMS, item_limit: int = NODE_BRIEF_ITEM_MAX_CHARS) -> list[str]:
    if value is None:
        raw_items: Sequence[Any] = ()
    elif isinstance(value, str):
        raw_items = value.replace("\r", "\n").split("\n")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        raw_items = (value,)
    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        item = cap_text(raw, item_limit)
        if item and item not in seen:
            seen.add(item)
            items.append(item)
        if len(items) >= limit:
            break
    return items


def bounded_task_brief(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    brief = {
        "background": cap_text(
            raw.get("background")
            or raw.get("context")
            or raw.get("business_context")
            or raw.get("businessContext"),
            NODE_BRIEF_BACKGROUND_MAX_CHARS,
        ),
        "execution": text_list(raw.get("execution") or raw.get("execution_steps") or raw.get("executionSteps")),
        "goal": cap_text(raw.get("goal") or raw.get("target") or raw.get("deliverable_goal") or raw.get("deliverableGoal"), NODE_BRIEF_GOAL_MAX_CHARS),
        "acceptance_criteria": text_list(raw.get("acceptance_criteria") or raw.get("acceptanceCriteria") or raw.get("verification")),
        "constraints": text_list(raw.get("constraints") or raw.get("requirements") or raw.get("must_not") or raw.get("mustNot")),
        "inputs": text_list(raw.get("inputs") or raw.get("references")),
        "deliverables": text_list(raw.get("deliverables") or raw.get("outputs")),
    }
    return {key: item for key, item in brief.items() if item not in ("", [])}


def task_brief_budget_violations(value: Any) -> list[str]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    violations: list[str] = []
    scalar_limits = {
        "background": NODE_BRIEF_BACKGROUND_MAX_CHARS,
        "goal": NODE_BRIEF_GOAL_MAX_CHARS,
    }
    for field, limit in scalar_limits.items():
        value_text = text(raw.get(field))
        if value_text and len(value_text) > limit:
            violations.append(f"task_brief.{field} is too long ({len(value_text)} chars); max {limit}.")
    for field in ("execution", "acceptance_criteria", "constraints", "inputs", "deliverables"):
        raw_items = raw.get(field)
        items = text_list(raw_items, limit=10_000, item_limit=10_000)
        if len(items) > NODE_BRIEF_LIST_MAX_ITEMS:
            violations.append(f"task_brief.{field} has too many items ({len(items)}); max {NODE_BRIEF_LIST_MAX_ITEMS}.")
        for index, item in enumerate(items, start=1):
            if len(item) > NODE_BRIEF_ITEM_MAX_CHARS:
                violations.append(
                    f"task_brief.{field}[{index}] is too long ({len(item)} chars); max {NODE_BRIEF_ITEM_MAX_CHARS}."
                )
    return violations


def node_task_brief(node: Mapping[str, Any]) -> dict[str, Any]:
    node_metadata = metadata(node.get("metadata"))
    return bounded_task_brief(node_metadata.get("task_brief") or node_metadata.get("taskBrief"))


def _graph_nodes(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else []
    return [dict(node) for node in nodes if isinstance(node, Mapping)] if isinstance(nodes, list) else []


def _graph_edges(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    edges = graph.get("edges") if isinstance(graph, Mapping) else []
    return [dict(edge) for edge in edges if isinstance(edge, Mapping)] if isinstance(edges, list) else []


def _node_id(node: Mapping[str, Any]) -> str:
    return text(node.get("node_id") or node.get("id"))


def _mission_id(mission: Mapping[str, Any]) -> str:
    return text(mission.get("mission_id") or mission.get("missionId") or mission.get("id"))


def upstream_handoff_deliverables(graph: Mapping[str, Any], node_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    node_id = text(node_id)
    if not node_id:
        return []
    nodes = _graph_nodes(graph)
    edges = _graph_edges(graph)
    upstream_ids = [
        text(edge.get("from_node_id") or edge.get("source"))
        for edge in edges
        if text(edge.get("to_node_id") or edge.get("target")) == node_id
    ]
    if not upstream_ids:
        return []
    nodes_by_id = {_node_id(node): node for node in nodes}
    result: list[dict[str, Any]] = []
    for upstream_id in upstream_ids:
        upstream = nodes_by_id.get(upstream_id) or {}
        deliverable = upstream.get("deliverable") or upstream.get("last_deliverable") or upstream.get("lastDeliverable")
        if not isinstance(deliverable, Mapping):
            continue
        artifact_refs = deliverable.get("artifact_refs") or deliverable.get("artifactRefs")
        artifact_refs = artifact_refs if isinstance(artifact_refs, list) else []
        next_context = deliverable.get("next_context") or deliverable.get("nextContext")
        next_context = next_context if isinstance(next_context, Mapping) else {}
        result.append({
            "node_id": upstream_id,
            "title": cap_text(upstream.get("title"), NODE_TITLE_MAX_CHARS),
            "status": text(deliverable.get("status")),
            "result": text(deliverable.get("result")),
            "summary": cap_text(deliverable.get("summary"), WORKER_CONTEXT_FIELD_MAX_CHARS),
            "artifact_refs": artifact_refs[:8],
            "next_context": {
                key: value for key, value in {
                    "summary": cap_text(next_context.get("summary"), WORKER_CONTEXT_FIELD_MAX_CHARS),
                    "handoff_target": cap_text(next_context.get("handoff_target") or next_context.get("handoffTarget"), NODE_TITLE_MAX_CHARS),
                    "open_questions": text_list(next_context.get("open_questions") or next_context.get("openQuestions"), limit=4),
                    "constraints": text_list(next_context.get("constraints"), limit=4),
                }.items() if value not in ("", [])
            },
            "source": text(deliverable.get("source")),
        })
        if len(result) >= limit:
            break
    return result


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _json_loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except Exception:
        return fallback
    return parsed


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _event_node_id(event: Mapping[str, Any]) -> str:
    payload = _event_payload(event)
    subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
    payload_subject = payload.get("subject") if isinstance(payload.get("subject"), Mapping) else {}
    source_payload = payload.get("source_payload") if isinstance(payload.get("source_payload"), Mapping) else {}
    return text(
        event.get("node_id")
        or event.get("nodeId")
        or subject.get("node_id")
        or subject.get("nodeId")
        or payload.get("node_id")
        or payload.get("nodeId")
        or payload_subject.get("node_id")
        or payload_subject.get("nodeId")
        or source_payload.get("node_id")
        or source_payload.get("nodeId")
    )


def _terminal_excerpt(db: Any, run_id: str, *, limit: int = RECENT_EVENT_MAX_CHARS) -> dict[str, Any]:
    if not run_id or not getattr(db, "_conn", None):
        return {}
    with db._lock:
        row = db._conn.execute(
            """
            SELECT *
              FROM run_events
             WHERE run_id = ?
             ORDER BY seq DESC, id DESC
             LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    payload = payload_from_run_event_row(row)
    excerpt = cap_text(
        payload.get("summary")
        or payload.get("text")
        or payload.get("error")
        or payload.get("message"),
        limit,
    )
    return {
        key: value
        for key, value in {
            "event_type": text(_row_value(row, "event_type")),
            "status": text(payload.get("status")),
            "excerpt": excerpt,
            "seq": int(_row_value(row, "seq", 0) or 0),
        }.items()
        if value not in ("", 0)
    }


def prior_node_attempts(db: Any, mission_id: str, node_id: str, *, limit: int = PRIOR_ATTEMPTS_LIMIT) -> list[dict[str, Any]]:
    mission_id = text(mission_id)
    node_id = text(node_id)
    if not mission_id or not node_id or not getattr(db, "_conn", None):
        return []
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT b.run_id, b.session_id, b.runtime_session_id, b.role, b.created_at,
                   r.status AS run_status, r.updated_at AS run_updated_at
              FROM team_mission_run_bindings b
              LEFT JOIN runs r ON r.run_id = b.run_id
             WHERE b.mission_id = ? AND b.node_id = ?
             ORDER BY b.created_at DESC, b.run_id DESC
             LIMIT ?
            """,
            (mission_id, node_id, max(1, min(int(limit or PRIOR_ATTEMPTS_LIMIT), PRIOR_ATTEMPTS_LIMIT))),
        ).fetchall()
    result: list[dict[str, Any]] = []
    deliverable_getter = getattr(db, "latest_team_mission_deliverable_for_run", None)
    for row in rows:
        run_id = text(_row_value(row, "run_id"))
        deliverable = deliverable_getter(run_id) if callable(deliverable_getter) else {}
        deliverable = deliverable if isinstance(deliverable, Mapping) else {}
        terminal = _terminal_excerpt(db, run_id, limit=PRIOR_ATTEMPT_MAX_CHARS)
        result.append({
            key: value
            for key, value in {
                "run_id": run_id,
                "status": text(_row_value(row, "run_status")),
                "role": text(_row_value(row, "role")),
                "summary": cap_text(deliverable.get("summary") or terminal.get("excerpt"), PRIOR_ATTEMPT_MAX_CHARS),
                "result": text(deliverable.get("result")),
                "deliverable_status": text(deliverable.get("status")),
                "terminal_event": terminal.get("event_type"),
                "terminal_status": terminal.get("status"),
            }.items()
            if value not in ("", None)
        })
    return result


def recent_node_events(db: Any, mission_id: str, node_id: str, *, limit: int = RECENT_EVENTS_LIMIT) -> list[dict[str, Any]]:
    mission_id = text(mission_id)
    node_id = text(node_id)
    if not mission_id or not node_id:
        return []
    getter = getattr(db, "list_team_mission_events", None)
    if not callable(getter):
        return []
    try:
        bounded_limit = max(1, min(int(limit or RECENT_EVENTS_LIMIT), RECENT_EVENTS_LIMIT))
        events = getter(mission_id, limit=max(50, min(bounded_limit * 5, 500)))
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for event in reversed(events if isinstance(events, list) else []):
        if _event_node_id(event) != node_id:
            continue
        payload = _event_payload(event)
        source_payload = payload.get("source_payload") if isinstance(payload.get("source_payload"), Mapping) else {}
        result.append({
            key: value
            for key, value in {
                "seq": event.get("seq") or payload.get("seq"),
                "kind": text(payload.get("kind")),
                "event_type": text(payload.get("source_event_type") or payload.get("event_type") or event.get("type")),
                "status": text(source_payload.get("status") or payload.get("status")),
                "reason_code": text(payload.get("reason_code") or source_payload.get("reason_code")),
                "summary": cap_text(
                    source_payload.get("summary")
                    or source_payload.get("text")
                    or source_payload.get("message")
                    or payload.get("failure_message"),
                    RECENT_EVENT_MAX_CHARS,
                ),
            }.items()
            if value not in ("", None)
        })
        if len(result) >= bounded_limit:
            break
    return result


def _compact_node(node: Mapping[str, Any], *, include_brief: str = "none") -> dict[str, Any]:
    node_metadata = metadata(node.get("metadata"))
    result: dict[str, Any] = {
        "node_id": _node_id(node),
        "kind": text(node.get("kind")),
        "title": cap_text(node.get("title"), NODE_TITLE_MAX_CHARS),
        "objective": cap_text(node.get("objective"), NODE_OBJECTIVE_MAX_CHARS),
        "status": text(node.get("status")),
        "assignee_member_id": text(node.get("assignee_member_id") or node_metadata.get("assignee_member_id") or node_metadata.get("member_id")),
        "assignee_profile_id": text(node.get("assignee_profile_id")),
        "role": text(node_metadata.get("role")),
        "phase": text(node_metadata.get("phase")),
        "work_type": text(node_metadata.get("work_type") or node_metadata.get("original_kind")),
    }
    if include_brief == "summary":
        brief = node_task_brief(node)
        if brief:
            result["task_brief_summary"] = {
                "goal": cap_text(brief.get("goal"), 500),
                "acceptance_criteria": text_list(brief.get("acceptance_criteria"), limit=4, item_limit=240),
            }
    elif include_brief == "capped":
        brief = node_task_brief(node)
        if brief:
            result["task_brief"] = brief
    return {key: value for key, value in result.items() if value not in ("", [], {})}


def team_mission_graph_summary(graph: Mapping[str, Any], *, node_limit: int = GRAPH_SUMMARY_NODE_LIMIT) -> dict[str, Any]:
    mission = graph.get("mission") if isinstance(graph.get("mission"), Mapping) else {}
    nodes = _graph_nodes(graph)
    edges = _graph_edges(graph)
    status_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for node in nodes:
        status = text(node.get("status")) or "unknown"
        kind = text(node.get("kind")) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    compact_nodes = [_compact_node(node, include_brief="none") for node in nodes[: max(0, node_limit)]]
    summary = {
        "mission": {
            "mission_id": text(mission.get("mission_id")),
            "title": cap_text(mission.get("title"), 160),
            "objective": cap_text(mission.get("objective"), 800),
            "mode": text(mission.get("mode")),
            "status": text(mission.get("status")),
        },
        "node_count": len(nodes),
        "edge_count": len(edges),
        "status_counts": status_counts,
        "kind_counts": kind_counts,
        "nodes": compact_nodes,
        "nodes_omitted": max(0, len(nodes) - len(compact_nodes)),
    }
    if len(json.dumps(summary, ensure_ascii=False, separators=(",", ":"))) <= GRAPH_SUMMARY_BUDGET_CHARS:
        return summary
    summary["nodes"] = [
        {
            key: value
            for key, value in {
                "node_id": node.get("node_id"),
                "kind": node.get("kind"),
                "status": node.get("status"),
                "title": cap_text(node.get("title"), 80),
            }.items()
            if value not in ("", None)
        }
        for node in compact_nodes[:12]
    ]
    summary["nodes_omitted"] = max(0, len(nodes) - len(summary["nodes"]))
    summary["truncated"] = True
    return summary


def team_mission_graph_slice(
    graph: Mapping[str, Any],
    *,
    node_ids: Sequence[str] | None = None,
    include_dependencies: bool = True,
    include_brief: str = "summary",
    limit: int = GRAPH_SLICE_NODE_LIMIT,
) -> dict[str, Any]:
    requested = {text(node_id) for node_id in (node_ids or []) if text(node_id)}
    nodes = _graph_nodes(graph)
    edges = _graph_edges(graph)
    selected_ids: set[str] = set(requested)
    if include_dependencies and requested:
        for edge in edges:
            from_id = text(edge.get("from_node_id") or edge.get("source"))
            to_id = text(edge.get("to_node_id") or edge.get("target"))
            if from_id in requested or to_id in requested:
                selected_ids.add(from_id)
                selected_ids.add(to_id)
    selected_nodes = [
        node for node in nodes
        if (not selected_ids or _node_id(node) in selected_ids)
    ][: max(1, min(limit, GRAPH_SLICE_NODE_LIMIT))]
    selected_node_ids = {_node_id(node) for node in selected_nodes}
    selected_edges = [
        {
            "edge_id": text(edge.get("edge_id") or edge.get("id")),
            "from_node_id": text(edge.get("from_node_id") or edge.get("source")),
            "to_node_id": text(edge.get("to_node_id") or edge.get("target")),
            "kind": text(edge.get("kind")) or "depends_on",
        }
        for edge in edges
        if text(edge.get("from_node_id") or edge.get("source")) in selected_node_ids
        and text(edge.get("to_node_id") or edge.get("target")) in selected_node_ids
    ]
    return {
        "mission_id": text((graph.get("mission") if isinstance(graph.get("mission"), Mapping) else {}).get("mission_id")),
        "nodes": [_compact_node(node, include_brief=include_brief) for node in selected_nodes],
        "edges": selected_edges,
        "node_count": len(selected_nodes),
        "edge_count": len(selected_edges),
        "has_more": len(nodes) > len(selected_nodes) if not requested else False,
    }


def _append_list(lines: list[str], label: str, items: Any) -> None:
    values = text_list(items, limit=NODE_BRIEF_LIST_MAX_ITEMS, item_limit=NODE_BRIEF_ITEM_MAX_CHARS)
    if not values:
        return
    lines.append(f"{label}:")
    for item in values:
        lines.append(f"- {item}")
    lines.append("")


def _safe_json(value: Any, *, limit: int = WORKER_CONTEXT_FIELD_MAX_CHARS) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    except Exception:
        rendered = text(value)
    return cap_text(rendered, limit)


def _render_context_sections(sections: list[tuple[int, str, list[str]]], included: list[bool] | None = None) -> str:
    included = included if included is not None else [True] * len(sections)
    lines: list[str] = []
    for index, (_priority, _name, section_lines) in enumerate(sections):
        if included[index]:
            lines.extend(section_lines)
    return "\n".join(lines).strip()


def _fit_context_sections_to_budget(sections: list[tuple[int, str, list[str]]]) -> tuple[str, bool, list[str]]:
    included = [True] * len(sections)
    dropped: list[str] = []
    rendered = _render_context_sections(sections, included)
    if len(rendered) <= WORKER_CONTEXT_MAX_CHARS:
        return rendered, False, dropped
    for priority in sorted({priority for priority, _name, _lines in sections}, reverse=True):
        for index, (section_priority, section_name, _section_lines) in enumerate(sections):
            if section_priority != priority or not included[index]:
                continue
            if len(rendered) <= WORKER_CONTEXT_MAX_CHARS:
                break
            included[index] = False
            dropped.append(section_name)
            rendered = _render_context_sections(sections, included)
        if len(rendered) <= WORKER_CONTEXT_MAX_CHARS:
            break
    if len(rendered) > WORKER_CONTEXT_MAX_CHARS:
        rendered = cap_text(rendered, WORKER_CONTEXT_MAX_CHARS)
    return rendered, True, dropped


def build_team_mission_worker_context(
    *,
    mission: Mapping[str, Any],
    node: Mapping[str, Any],
    graph: Mapping[str, Any] | None = None,
    db: Any = None,
    memory_context: Mapping[str, Any] | None = None,
    memory_text: str = "",
) -> dict[str, Any]:
    mission = mission if isinstance(mission, Mapping) else {}
    node = node if isinstance(node, Mapping) else {}
    graph = graph if isinstance(graph, Mapping) else {}
    brief = node_task_brief(node)
    output_contract = metadata(node.get("output_contract"))
    node_metadata = metadata(node.get("metadata"))
    mission_title = cap_text(mission.get("title") or node_metadata.get("task_title"), NODE_TITLE_MAX_CHARS)
    mission_objective = cap_text(mission.get("objective") or node_metadata.get("task_objective"), WORKER_CONTEXT_BODY_MAX_CHARS)
    node_title = cap_text(node.get("title"), NODE_TITLE_MAX_CHARS)
    node_objective = cap_text(node.get("objective") or mission_objective or node_title, NODE_OBJECTIVE_MAX_CHARS)
    background = cap_text(
        brief.get("background")
        or f"This node is part of the team task '{mission_title or mission_objective or node_title}'.",
        WORKER_CONTEXT_BODY_MAX_CHARS,
    )
    execution = brief.get("execution") or [node_objective or "Complete the assigned node work."]
    goal = cap_text(brief.get("goal") or output_contract.get("goal") or node_objective, WORKER_CONTEXT_FIELD_MAX_CHARS)
    acceptance = brief.get("acceptance_criteria") or output_contract.get("acceptance_criteria") or [
        "The result directly satisfies the assigned node objective.",
        "The response names assumptions, unresolved questions, and verification performed.",
    ]
    graph_summary = team_mission_graph_summary(graph) if graph else {}
    sections: list[tuple[int, str, list[str]]] = []
    sections.append((1, "identity", [
        "You are executing one assigned node in a DoXie team task.",
        "Stay inside this node's scope. Produce a concrete result the Team Leader can verify and synthesize.",
        "If required input is missing, the scope is ambiguous, or an acceptance criterion cannot be verified, call the clarify tool with the specific question before proceeding. Do not execute on guessed assumptions.",
        "",
        "Mission:",
        f"- Title: {mission_title or '(not specified)'}",
        f"- Objective: {mission_objective or '(not specified)'}",
        f"- Status: {text(mission.get('status')) or '(not specified)'}",
        "",
        "Assigned node:",
        f"- Id: {_node_id(node) or '(not specified)'}",
        f"- Title: {node_title or '(not specified)'}",
        f"- Kind: {text(node.get('kind')) or 'worker'}",
        f"- Objective: {node_objective or '(not specified)'}",
        "",
    ]))
    sections.append((4, "background", [
        "Background:",
        background,
        "",
    ]))
    execution_lines: list[str] = []
    _append_list(execution_lines, "Execution", execution)
    if execution_lines:
        sections.append((4, "execution", execution_lines))
    sections.append((2, "goal", ["Goal:", goal or node_objective, ""]))
    input_lines: list[str] = []
    _append_list(input_lines, "Inputs", brief.get("inputs"))
    if input_lines:
        sections.append((4, "inputs", input_lines))
    handoffs = upstream_handoff_deliverables(graph, _node_id(node)) if graph else []
    if handoffs:
        sections.append((5, "upstream_handoffs", [
            "Upstream handoff deliverables:",
            _safe_json(handoffs[:3], limit=WORKER_CONTEXT_FIELD_MAX_CHARS),
            "",
        ]))
    deliverable_lines: list[str] = []
    _append_list(deliverable_lines, "Deliverables", brief.get("deliverables") or output_contract.get("deliverables"))
    if deliverable_lines:
        sections.append((3, "deliverables", deliverable_lines))
    constraint_lines: list[str] = []
    _append_list(constraint_lines, "Constraints", brief.get("constraints"))
    if constraint_lines:
        sections.append((4, "constraints", constraint_lines))
    acceptance_lines = ["Acceptance criteria:"]
    for item in text_list(acceptance, limit=NODE_BRIEF_LIST_MAX_ITEMS, item_limit=NODE_BRIEF_ITEM_MAX_CHARS):
        acceptance_lines.append(f"- {item}")
    acceptance_lines.append("")
    sections.append((2, "acceptance", acceptance_lines))
    if output_contract:
        sections.append((2, "output_contract", ["Output contract:", _safe_json(output_contract), ""]))
        if text(output_contract.get("delivery_channel") or output_contract.get("deliveryChannel")).lower() == "handoff":
            sections.append((1, "handoff_protocol", [
                "Handoff protocol:",
                "- Stream normal user-visible progress and conclusions in natural language.",
                "- Before your final visible response, call team_mission_submit_deliverable with the structured payload that satisfies the output contract.",
                "- Do not print the structured JSON deliverable in the assistant message; the tool stores it for downstream nodes.",
                "",
            ]))
    if graph_summary:
        sections.append((7, "graph_summary", ["Mission graph summary:", _safe_json(graph_summary, limit=WORKER_CONTEXT_FIELD_MAX_CHARS), ""]))
    if db is not None:
        prior = prior_node_attempts(db, _mission_id(mission), _node_id(node), limit=PRIOR_ATTEMPTS_LIMIT)
        if prior:
            sections.append((
                5,
                "prior_attempts",
                [
                    "Prior attempts on this node:",
                    _safe_json(prior, limit=PRIOR_ATTEMPTS_LIMIT * PRIOR_ATTEMPT_MAX_CHARS),
                    "",
                ],
            ))
        recent = recent_node_events(db, _mission_id(mission), _node_id(node), limit=RECENT_EVENTS_LIMIT)
        if recent:
            sections.append((
                5,
                "recent_events",
                [
                    "Recent events:",
                    _safe_json(recent, limit=RECENT_EVENTS_LIMIT * RECENT_EVENT_MAX_CHARS),
                    "",
                ],
            ))
    if memory_text:
        sections.append((6, "team_memory", ["Team memory summary:", cap_text(memory_text, WORKER_CONTEXT_FIELD_MAX_CHARS), ""]))
    rendered, truncated, dropped_sections = _fit_context_sections_to_budget(sections)
    return {
        "kind": "team_mission_worker_context",
        "text": rendered,
        "text_chars": len(rendered),
        "truncated": truncated,
        "dropped_sections": dropped_sections,
        "task_brief": brief,
        "graph_summary": graph_summary,
        "memory": dict(memory_context or {}),
        "caps": {
            "worker_context_max_chars": WORKER_CONTEXT_MAX_CHARS,
            "field_max_chars": WORKER_CONTEXT_FIELD_MAX_CHARS,
            "body_max_chars": WORKER_CONTEXT_BODY_MAX_CHARS,
        },
    }
