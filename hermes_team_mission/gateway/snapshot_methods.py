# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from typing import Any

from .common import *
from .public_conversation_identity import (
    public_team_conversation,
    public_team_mission_graph,
)
from hermes_team_mission.read_model import build_team_mission_read_model


SNAPSHOT_SCHEMA_VERSION = "2026-06-29"


def _activity_id_from_params(params: dict[str, Any]) -> str:
    return str(params.get("activity_id") or params.get("activityId") or "").strip()


def _mission_id_from_activity_id(activity_id: str) -> str:
    prefix = "mission:"
    activity_id = str(activity_id or "").strip()
    return activity_id[len(prefix):].strip() if activity_id.startswith(prefix) else ""


def _latest_team_mission_event_seq(db: Any, mission_id: str) -> int:
    try:
        events = db.runs.list_events_by_mission_activity(
            mission_id,
            after_seq=0,
            limit=1,
            reverse=True,
        )
        return max((int(event.get("seq") or 0) for event in events), default=0)
    except Exception:
        return 0


def _result_for_graph(db: Any, mission_id: str, graph: dict[str, Any]) -> dict[str, Any]:
    result = graph.get("result") if isinstance(graph.get("result"), dict) else {}
    if result:
        return result
    getter = getattr(db, "get_team_mission_result", None)
    if callable(getter):
        try:
            result = getter(mission_id) or {}
        except Exception:
            result = {}
    return result if isinstance(result, dict) else {}


def _pending_approvals(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    approvals: list[dict[str, Any]] = []
    for node in nodes:
        kind = str(node.get("kind") or "").strip()
        status = str(node.get("status") or "").strip().lower()
        if kind != "approval_gate" or status in {"completed", "verified", "cancelled", "canceled", "failed"}:
            continue
        approvals.append({
            "node_id": str(node.get("node_id") or ""),
            "nodeId": str(node.get("node_id") or ""),
            "status": status or str(node.get("status") or ""),
            "title": str(node.get("title") or ""),
        })
    return approvals


def _snapshot_version(mission_id: str, event_seq: int, graph: dict[str, Any], result: dict[str, Any]) -> str:
    mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
    updated_at = (
        result.get("updated_at")
        or result.get("updatedAt")
        or mission.get("updated_at")
        or mission.get("updatedAt")
        or ""
    )
    return f"mission:{mission_id}:seq:{int(event_seq or 0)}:updated:{updated_at}"


def _conversation_snapshot_version(conversation_session_id: str, conversation: dict[str, Any]) -> str:
    updated_at = (
        conversation.get("updated_at")
        or conversation.get("updatedAt")
        or conversation.get("created_at")
        or conversation.get("createdAt")
        or ""
    )
    return f"conversation:{conversation_session_id}:no-mission:updated:{updated_at}"


def _canonical_team_conversation_snapshot(
    db: Any,
    identifier: str,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identifier = str(identifier or "").strip()
    if not identifier:
        return {}
    graph = graph if isinstance(graph, dict) else {}
    conversation = graph.get("conversation") if isinstance(graph.get("conversation"), dict) else {}
    if not conversation:
        session_getter = getattr(db, "get_team_mission_conversation_by_session", None)
        if callable(session_getter):
            conversation = session_getter(identifier) or {}
        resolver = getattr(db, "resolve_team_mission_conversation", None)
        resolved = resolver(identifier) if not conversation and callable(resolver) else {}
        if not conversation and isinstance(resolved, dict):
            conversation = resolved.get("conversation") or {}
        if not graph and isinstance(resolved, dict) and isinstance(resolved.get("graph"), dict):
            graph = resolved["graph"]
    if not isinstance(conversation, dict) or not conversation:
        return {}
    conversation_session_id = str(
        conversation.get("conversation_session_id")
        or conversation.get("conversationSessionId")
        or identifier
    ).strip()
    public_conversation = public_team_conversation(conversation)
    team = _team_detail_projection_for_conversation(db, conversation, {})
    version = _conversation_snapshot_version(conversation_session_id, conversation)
    graph_payload = public_team_mission_graph({
        **graph,
        "mission": {},
        "conversation": public_conversation,
        "nodes": graph.get("nodes") if isinstance(graph.get("nodes"), list) else [],
        "edges": graph.get("edges") if isinstance(graph.get("edges"), list) else [],
        "run_bindings": graph.get("run_bindings") if isinstance(graph.get("run_bindings"), list) else [],
        "deliverables": graph.get("deliverables") if isinstance(graph.get("deliverables"), list) else [],
        "result": {},
    })
    if team:
        graph_payload["team"] = team
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "mission_id": "",
        "missionId": "",
        "activity_id": "",
        "activityId": "",
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        "snapshot_version": version,
        "snapshotVersion": version,
        "last_event_seq": 0,
        "lastEventSeq": 0,
        "mission": {},
        "conversation": public_conversation,
        "team": team,
        "nodes": graph_payload["nodes"],
        "edges": graph_payload["edges"],
        "run_bindings": graph_payload["run_bindings"],
        "runBindings": graph_payload["run_bindings"],
        "deliverables": graph_payload["deliverables"],
        "result": {},
        "pending_approvals": [],
        "pendingApprovals": [],
        "graph": graph_payload,
    }
    snapshot["read_model"] = build_team_mission_read_model(snapshot)
    return snapshot


def _canonical_team_mission_snapshot(db: Any, mission_id: str, graph: dict[str, Any] | None = None) -> dict[str, Any]:
    mission_id = str(mission_id or "").strip()
    if not mission_id:
        return {}
    graph = graph if isinstance(graph, dict) else db.team_mission_graphs.get_team_mission_graph(mission_id)
    if not isinstance(graph, dict) or not graph:
        return {}
    mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
    if not mission:
        return {}
    conversation = graph.get("conversation") if isinstance(graph.get("conversation"), dict) else {}
    public_conversation = public_team_conversation(conversation)
    public_graph = public_team_mission_graph(graph)
    team = _team_detail_projection_for_conversation(db, conversation, mission)
    nodes = [node for node in graph.get("nodes") or [] if isinstance(node, dict)]
    edges = [edge for edge in graph.get("edges") or [] if isinstance(edge, dict)]
    deliverables = [item for item in graph.get("deliverables") or [] if isinstance(item, dict)]
    result = _result_for_graph(db, mission_id, graph)
    latest_seq = _latest_team_mission_event_seq(db, mission_id)
    activity_id = f"mission:{mission_id}"
    version = _snapshot_version(mission_id, latest_seq, graph, result)
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "mission_id": mission_id,
        "missionId": mission_id,
        "activity_id": activity_id,
        "activityId": activity_id,
        "snapshot_version": version,
        "snapshotVersion": version,
        "last_event_seq": latest_seq,
        "lastEventSeq": latest_seq,
        "mission": public_graph.get("mission") if isinstance(public_graph.get("mission"), dict) else {},
        "conversation": public_conversation,
        "team": team,
        "nodes": nodes,
        "edges": edges,
        "run_bindings": graph.get("run_bindings") if isinstance(graph.get("run_bindings"), list) else [],
        "runBindings": graph.get("run_bindings") if isinstance(graph.get("run_bindings"), list) else [],
        "deliverables": deliverables,
        "result": result,
        "pending_approvals": _pending_approvals(nodes),
        "pendingApprovals": _pending_approvals(nodes),
        "graph": {
            **public_graph,
            **({"team": team} if team else {}),
            "result": result,
        },
    }
    snapshot["read_model"] = build_team_mission_read_model(snapshot)
    return snapshot


def _graph_for_params(db: Any, params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    mission_id = _mission_id_from_params(params) or _mission_id_from_activity_id(_activity_id_from_params(params))
    conversation_session_id = _conversation_session_id_from_params(params)
    legacy_conversation_id = _conversation_id_from_params(params)
    identifier = conversation_session_id or legacy_conversation_id
    if identifier:
        resolved = db.resolve_team_mission_conversation(identifier)
        conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
        if not conversation and conversation_session_id:
            conversation = db.get_team_mission_conversation_by_session(
                conversation_session_id
            ) or {}
        conversation_id = str((conversation or {}).get("conversation_id") or "").strip()
        conversation_session_id = str(
            (conversation or {}).get("conversation_session_id")
            or conversation_session_id
            or ""
        ).strip()
        if not conversation_id:
            return "", {}, {}
        graph = db.team_mission_graphs.get_team_mission_conversation_graph(conversation_id)
        if not graph:
            return "", {}, {
                "conversation_session_id": conversation_session_id,
            }
        mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
        resolved_mission_id = str(
            mission.get("mission_id")
            or mission.get("missionId")
            or mission_id
            or ""
        ).strip()
        return resolved_mission_id, graph, {
            "conversation_session_id": conversation_session_id,
        }
    if mission_id:
        return mission_id, db.team_mission_graphs.get_team_mission_graph(mission_id), {}
    return "", {}, {}


@method("team_mission.snapshot.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    params = params if isinstance(params, dict) else {}
    mission_id, graph, meta = _graph_for_params(db, params)
    if not mission_id:
        conversation_session_id = str(
            meta.get("conversation_session_id")
            or _conversation_session_id_from_params(params)
            or ""
        ).strip()
        legacy_identifier = _conversation_id_from_params(params)
        identifier = conversation_session_id or legacy_identifier
        if identifier:
            snapshot = _canonical_team_conversation_snapshot(db, identifier, graph)
            if not snapshot:
                return _err(rid, 4040, "team mission conversation not found")
            return _ok(
                rid,
                {
                    "mission_id": "",
                    "activity_id": "",
                    "conversation_session_id": snapshot["conversation_session_id"],
                    "snapshot": snapshot,
                    "read_model": snapshot["read_model"],
                    "snapshot_version": snapshot["snapshot_version"],
                    "last_event_seq": snapshot["last_event_seq"],
                },
            )
        return _err(rid, 4006, "mission_id, activity_id, or conversation_session_id required")
    snapshot = _canonical_team_mission_snapshot(db, mission_id, graph)
    if not snapshot:
        return _err(rid, 4040, "team mission not found")
    result = {
        "mission_id": mission_id,
        "activity_id": snapshot["activity_id"],
        "snapshot": snapshot,
        "read_model": snapshot["read_model"],
        "snapshot_version": snapshot["snapshot_version"],
        "last_event_seq": snapshot["last_event_seq"],
    }
    result.update(meta)
    return _ok(rid, result)


@method("team_mission.result.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    params = params if isinstance(params, dict) else {}
    mission_id = _mission_id_from_params(params) or _mission_id_from_activity_id(_activity_id_from_params(params))
    if not mission_id:
        return _err(rid, 4006, "mission_id or activity_id required")
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    if not graph:
        return _err(rid, 4040, "team mission not found")
    result = _result_for_graph(db, mission_id, graph)
    latest_seq = _latest_team_mission_event_seq(db, mission_id)
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "activity_id": f"mission:{mission_id}",
            "ready": bool(result),
            "status": "ready" if result else "pending",
            "result": result,
            "last_event_seq": latest_seq,
            "snapshot_version": _snapshot_version(mission_id, latest_seq, graph, result),
        },
    )
