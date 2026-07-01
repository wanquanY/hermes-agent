from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List

from hermes_team_mission.domain.identities import canonical_node_id as _canonical_graph_node_id
from hermes_team_mission.runtime.failure import classify_team_mission_failure


TEAM_MISSION_EVENT_PROTOCOL = "team_mission.event.v1"
TEAM_MISSION_RUNTIME_EVENT_TYPE = "team_mission.runtime.event"
TEAM_MISSION_CONVERSATION_STATUS_EVENT_TYPE = "team_mission.conversation.status"
logger = logging.getLogger(__name__)
_listener_lock = threading.RLock()
_event_listeners: list[Any] = []


def register_team_mission_event_listener(callback: Any) -> None:
    if not callable(callback):
        return
    with _listener_lock:
        if callback not in _event_listeners:
            _event_listeners.append(callback)


def notify_team_mission_event_listeners(mission_id: str, event: Dict[str, Any]) -> None:
    with _listener_lock:
        listeners = list(_event_listeners)
    for listener in listeners:
        try:
            listener(mission_id, event)
        except Exception:
            logger.debug("failed to notify Team Mission event listener", exc_info=True)


def text(value: Any) -> str:
    return str(value or "").strip()


def raw_text(value: Any) -> str:
    return "" if value is None else str(value)


def mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def array(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def event_payload(event: Dict[str, Any] | None) -> Dict[str, Any]:
    event = event if isinstance(event, dict) else {}
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _emit_team_event_log_diagnostic(stage: str, **fields: Any) -> None:
    try:
        from agent.dovie_diagnostics import emit_dovie_diagnostic

        emit_dovie_diagnostic("[dovie-team-event-log-debug]", {"stage": stage, **fields})
    except Exception:
        pass


def _text_stream_summary(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event_payload(event)
    text_value = raw_text(payload.get("delta") or payload.get("text") or payload.get("snapshot"))
    return {
        "event_type": source_event_type(event),
        "text_len": len(text_value),
        "text_preview": text_value[:80].replace("\n", "\\n"),
        "mode": text(payload.get("mode")),
        "status": text(payload.get("status")),
    }


def _first_text(*values: Any) -> str:
    for value in values:
        candidate = text(value)
        if candidate:
            return candidate
    return ""


def _payload_node(payload: Dict[str, Any]) -> Dict[str, Any]:
    node = mapping(payload.get("node"))
    if node:
        return node
    nodes = array(payload.get("nodes"))
    for item in nodes:
        node = mapping(item)
        if node:
            return node
    return {}


def _payload_edge(payload: Dict[str, Any]) -> Dict[str, Any]:
    edge = mapping(payload.get("edge"))
    if edge:
        return edge
    edges = array(payload.get("edges"))
    for item in edges:
        edge = mapping(item)
        if edge:
            return edge
    return {}


def _subject_node_id_from_event(source_event: Dict[str, Any], identity: Dict[str, str]) -> str:
    payload = event_payload(source_event)
    node = _payload_node(payload)
    return _first_text(
        node.get("node_id"),
        node.get("nodeId"),
        node.get("id"),
        payload.get("node_id"),
        payload.get("nodeId"),
        identity.get("node_id"),
        identity.get("nodeId"),
    )


def _canonical_subject(source_event: Dict[str, Any], identity: Dict[str, str]) -> Dict[str, Any]:
    payload = event_payload(source_event)
    event_type = source_event_type(source_event)
    mission_id = _first_text(identity.get("mission_id"), identity.get("missionId"), payload.get("mission_id"), payload.get("missionId"))
    conversation_id = _first_text(identity.get("conversation_id"), identity.get("conversationId"), payload.get("conversation_id"), payload.get("conversationId"))
    conversation_stable_session_id = _first_text(identity.get("stable_session_id"), identity.get("stableSessionId"), payload.get("stable_session_id"), payload.get("stableSessionId"))
    runtime_stable_session_id = _first_text(
        source_event.get("stored_session_id"),
        source_event.get("storedSessionId"),
        payload.get("stored_session_id"),
        payload.get("storedSessionId"),
        payload.get("session_key"),
        payload.get("sessionKey"),
        identity.get("runtime_stable_session_id"),
        identity.get("runtimeStableSessionId"),
    )
    runtime_session_id = _first_text(
        source_event.get("session_id"),
        source_event.get("sessionId"),
        payload.get("runtime_session_id"),
        payload.get("runtimeSessionId"),
        identity.get("runtime_session_id"),
        identity.get("runtimeSessionId"),
    )
    runtime_scope_key = _first_text(
        source_event.get("runtime_scope_key"),
        source_event.get("runtimeScopeKey"),
        payload.get("runtime_scope_key"),
        payload.get("runtimeScopeKey"),
        identity.get("runtime_scope_key"),
        identity.get("runtimeScopeKey"),
    )
    node_id = _subject_node_id_from_event(source_event, identity)
    node_kind = _first_text(identity.get("node_kind"), identity.get("nodeKind"), payload.get("node_kind"), payload.get("nodeKind"))
    output_contract_format = _first_text(
        identity.get("output_contract_format"),
        identity.get("outputContractFormat"),
        payload.get("output_contract_format"),
        payload.get("outputContractFormat"),
    )
    run_id = _first_text(source_event.get("run_id"), source_event.get("runId"), payload.get("run_id"), payload.get("runId"))
    turn_id = _first_text(source_event.get("turn_id"), source_event.get("turnId"), payload.get("turn_id"), payload.get("turnId"))

    subject: Dict[str, Any] = {
        "mission_id": mission_id,
        "conversation_id": conversation_id,
        "stable_session_id": conversation_stable_session_id,
        "conversation_stable_session_id": conversation_stable_session_id,
        "conversation_session_id": conversation_stable_session_id,
        "runtime_stable_session_id": runtime_stable_session_id,
        "source_session_id": runtime_stable_session_id,
        "runtime_session_id": runtime_session_id,
        "runtime_scope_key": runtime_scope_key,
        "run_id": run_id,
        "turn_id": turn_id,
    }
    if event_type == "mission.approval.requested":
        approval_id = _first_text(
            payload.get("approval_id"),
            payload.get("approvalId"),
            payload.get("id"),
            node_id,
        )
        subject.update({
            "type": "approval",
            "id": approval_id,
            "approval_id": approval_id,
            "node_id": approval_id,
            # CR-P3.3: graph identity only; for speaker use participant_id.
            "canonical_node_id": _canonical_graph_node_id(mission_id, approval_id),
        })
        return {key: value for key, value in subject.items() if text(value)}

    if event_type == "mission.edge.created":
        edge = _payload_edge(payload)
        from_node_id = _first_text(edge.get("from_node_id"), edge.get("fromNodeId"), edge.get("source"), edge.get("from"), payload.get("from_node_id"), payload.get("fromNodeId"))
        to_node_id = _first_text(edge.get("to_node_id"), edge.get("toNodeId"), edge.get("target"), edge.get("to"), payload.get("to_node_id"), payload.get("toNodeId"))
        edge_id = _first_text(edge.get("edge_id"), edge.get("edgeId"), edge.get("id"), f"{from_node_id}->{to_node_id}" if from_node_id and to_node_id else "")
        subject.update({
            "type": "edge",
            "id": edge_id,
            "edge_id": edge_id,
            "from_node_id": from_node_id,
            "to_node_id": to_node_id,
        })
        return {key: value for key, value in subject.items() if text(value)}

    if node_id:
        subject.update({
            "type": "node",
            "id": node_id,
            "node_id": node_id,
            # CR-P3.3: graph identity only; for speaker use participant_id.
            "canonical_node_id": _canonical_graph_node_id(mission_id, node_id),
            "node_kind": node_kind,
            "output_contract_format": output_contract_format,
            "task_id": _first_text(identity.get("task_id"), identity.get("taskId"), payload.get("task_id"), payload.get("taskId")),
            "task_frame_id": _first_text(identity.get("task_frame_id"), identity.get("taskFrameId"), payload.get("task_frame_id"), payload.get("taskFrameId")),
        })
        return {key: value for key, value in subject.items() if text(value)}

    subject.update({
        "type": "mission",
        "id": mission_id,
    })
    return {key: value for key, value in subject.items() if text(value)}


def _payload_stream_fragment(payload: Dict[str, Any]) -> str:
    if "delta" in payload:
        return raw_text(payload.get("delta"))
    if "text" in payload:
        return raw_text(payload.get("text"))
    if "snapshot" in payload:
        return raw_text(payload.get("snapshot"))
    return raw_text(payload.get("output"))


def _is_snapshot_message_delta(event: Dict[str, Any] | None) -> bool:
    if source_event_type(event) != "message.delta":
        return False
    payload = event_payload(event)
    return text(payload.get("mode")).lower() == "snapshot"


def _payload_int(payload: Dict[str, Any], key: str) -> int | None:
    if key not in payload:
        return None
    try:
        return int(payload.get(key))
    except (TypeError, ValueError):
        return None


def _text_stream_contract(source_event: Dict[str, Any], subject: Dict[str, Any]) -> Dict[str, Any]:
    event_type = source_event_type(source_event)
    if event_type not in {"message.start", "message.delta", "message.complete"}:
        return {}
    payload = event_payload(source_event)
    mode = text(payload.get("mode")).lower()
    if event_type == "message.delta":
        if _is_snapshot_message_delta(source_event):
            return {}
        if mode in {"", "append"}:
            mode = "append"
    stream_id = _first_text(
        payload.get("stream_id"),
        payload.get("streamId"),
        f"{subject.get('runtime_stable_session_id') or subject.get('stable_session_id')}:{subject.get('node_id') or subject.get('id')}:{subject.get('run_id')}:assistant",
    )
    fragment = _payload_stream_fragment(payload)
    contract: Dict[str, Any] = {
        "stream_id": stream_id,
        "subject": subject,
        "event": event_type.replace("message.", ""),
        "mode": mode,
        "run_id": subject.get("run_id", ""),
        "turn_id": subject.get("turn_id", ""),
    }
    if event_type == "message.delta":
        contract["delta"] = fragment
        contract["text"] = fragment
        offset = _payload_int(payload, "offset")
        if offset is not None:
            contract["offset"] = offset
    elif event_type == "message.complete":
        complete_text = raw_text(
            payload.get("text")
            if "text" in payload
            else payload.get("final_response")
            if "final_response" in payload
            else payload.get("finalResponse")
            if "finalResponse" in payload
            else payload.get("summary")
            if "summary" in payload
            else payload.get("message")
        )
        if complete_text:
            contract["text"] = complete_text
        status = text(payload.get("status"))
        if status:
            contract["status"] = status
    return {key: value for key, value in contract.items() if value is not None and (not isinstance(value, str) or value != "")}


def _subject_node_id(subject: Dict[str, Any]) -> str:
    subject_type = text(subject.get("type")).lower()
    if subject_type == "approval":
        return _first_text(subject.get("approval_id"), subject.get("approvalId"), subject.get("node_id"), subject.get("nodeId"), subject.get("id"))
    if subject_type == "node":
        return _first_text(subject.get("node_id"), subject.get("nodeId"), subject.get("id"))
    return ""


def _subject_canonical_node_id(subject: Dict[str, Any]) -> str:
    return _first_text(subject.get("canonical_node_id"), subject.get("canonicalNodeId"))


def _apply_subject_node_identity(target: Dict[str, Any], subject: Dict[str, Any]) -> None:
    node_id = _subject_node_id(subject)
    if not node_id:
        return
    target["node_id"] = node_id
    target["nodeId"] = node_id
    canonical_node_id = _subject_canonical_node_id(subject)
    if canonical_node_id:
        # CR-P3.3: graph identity only; for speaker use participant_id.
        target["canonical_node_id"] = canonical_node_id
        target["canonicalNodeId"] = canonical_node_id
    if text(subject.get("type")).lower() == "approval":
        approval_id = _first_text(subject.get("approval_id"), subject.get("approvalId"), subject.get("id"), node_id)
        if approval_id:
            target["approval_id"] = approval_id
            target["approvalId"] = approval_id


def _tool_identity_contract(source_event: Dict[str, Any]) -> Dict[str, Any]:
    event_type = source_event_type(source_event)
    if event_type not in {"tool.start", "tool.progress", "tool.generating", "tool.complete"}:
        return {}
    payload = event_payload(source_event)
    tool = mapping(payload.get("tool"))
    function = mapping(payload.get("function") or tool.get("function"))
    name = _first_text(
        payload.get("name"),
        payload.get("tool_name"),
        payload.get("toolName"),
        payload.get("tool"),
        tool.get("name"),
        function.get("name"),
        source_event.get("name"),
        source_event.get("tool_name"),
        source_event.get("toolName"),
    )
    call_id = _first_text(
        payload.get("tool_call_id"),
        payload.get("toolCallId"),
        payload.get("tool_id"),
        payload.get("toolId"),
        payload.get("id"),
        source_event.get("tool_call_id"),
        source_event.get("toolCallId"),
        source_event.get("tool_id"),
        source_event.get("toolId"),
    )
    contract: Dict[str, Any] = {}
    if name:
        contract.update({
            "name": name,
            "tool_name": name,
            "toolName": name,
        })
    if call_id:
        contract.update({
            "tool_call_id": call_id,
            "toolCallId": call_id,
            "tool_id": call_id,
            "toolId": call_id,
        })
    return contract


def source_event_type(event: Dict[str, Any] | None) -> str:
    return text((event or {}).get("type"))


def event_seq(event: Dict[str, Any] | None) -> int:
    event = event if isinstance(event, dict) else {}
    for key in ("source_seq", "sourceSeq", "seq"):
        try:
            value = int(event.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    payload = event_payload(event)
    for key in ("source_seq", "sourceSeq", "seq"):
        try:
            value = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def mission_event_kind(source_event: Dict[str, Any], identity: Dict[str, str] | None = None) -> str:
    event_type = source_event_type(source_event)
    identity = identity or {}
    payload = event_payload(source_event)
    status = text(payload.get("status")).lower()
    node_kind = text(
        identity.get("node_kind")
        or identity.get("nodeKind")
        or payload.get("node_kind")
        or payload.get("nodeKind")
    ).lower()
    output_contract_format = text(
        identity.get("output_contract_format")
        or identity.get("outputContractFormat")
        or payload.get("output_contract_format")
        or payload.get("outputContractFormat")
    ).lower()
    is_final_deliverable = (
        node_kind in {"synthesis", "synthesizer", "finalizer"}
        or output_contract_format in {"final_deliverable", "final-deliverable"}
        or payload.get("team_mission_final_deliverable") is True
        or payload.get("teamMissionFinalDeliverable") is True
    )
    if event_type == "mission.strategy.actions":
        return "plan.updated"
    if event_type == "mission.node.created":
        return "node.created"
    if event_type == "mission.node.updated":
        return "node.updated"
    if event_type == "mission.node.started":
        return "node.started"
    if event_type == "mission.node.run.bound":
        return "node.bound"
    if event_type == "mission.node.deliverable.recorded":
        return "node.deliverable.recorded"
    if event_type == "mission.node.finished":
        if status in {"blocked", "partial"}:
            return "node.blocked"
        if status in {"failed", "error"}:
            return "node.failed"
        if status in {"cancelled", "canceled", "interrupted"}:
            return "node.interrupted"
        return "node.completed"
    if event_type == "mission.result.recorded":
        return "mission.result.recorded"
    if event_type == "mission.report.ready":
        return "mission.report.ready"
    if event_type == "mission.snapshot.updated":
        return "mission.snapshot.updated"
    if event_type == "mission.node.blocked":
        return "node.blocked"
    if event_type == "mission.node.failed":
        return "node.failed"
    if event_type == "mission.edge.created":
        return "edge.created"
    if event_type == "mission.approval.requested":
        return "approval.requested"
    if event_type == "mission.plan.rejected":
        return "approval.rejected"
    if event_type == "mission.memory.compiled":
        return "memory.compiled"
    if event_type in {"message.delta", "subagent.output_delta"}:
        return "final.output.delta" if is_final_deliverable else "node.output.delta"
    if event_type == "message.complete":
        if status in {"error", "failed"}:
            return "node.failed"
        if status in {"cancelled", "canceled", "interrupted"}:
            return "node.interrupted"
        return "final.completed" if is_final_deliverable else "node.completed"
    if event_type in {"error"}:
        return "node.failed"
    if event_type in {"session.interrupted", "session.recalled"}:
        return "node.interrupted"
    return "runtime.trace"


def projection_event(
    source_event: Dict[str, Any],
    identity: Dict[str, str],
    *,
    source_seq: int = 0,
    mission_seq: int = 0,
) -> Dict[str, Any]:
    source_event = dict(source_event or {})
    source_payload = event_payload(source_event)
    event_type = source_event_type(source_event)
    timestamp = float(source_event.get("timestamp") or time.time())
    subject = _canonical_subject(source_event, identity)
    text_stream = _text_stream_contract(source_event, subject)
    tool_identity = _tool_identity_contract(source_event)
    # Keep exactly one copy of each field. The previous projection duplicated
    # source_event 4x (source_event/sourceEvent/runtime_event/runtimeEvent),
    # source_payload 2x and text_stream 2x, bloating every streamed delta to
    # ~12-15KB. All Dovie/reconcile consumers read the snake_case primary first
    # (with camelCase only as a historical fallback), and derive source_payload
    # from source_event, so the duplicates are pure transport overhead that made
    # live streaming chunky. source_event is retained for diagnostics per the
    # ABI spec.
    payload: Dict[str, Any] = {
        "protocol": TEAM_MISSION_EVENT_PROTOCOL,
        "kind": mission_event_kind(source_event, identity),
        "subject": subject,
        "event_type": event_type,
        "source_event_type": event_type,
        "source_event": source_event,
        "source_payload": dict(source_payload),
    }
    failure = classify_team_mission_failure(event_type, source_payload)
    if failure:
        payload["reason_code"] = failure["reason_code"]
        payload["recoverability"] = failure["recoverability"]
        payload["failure_message"] = failure["message"]
    payload.update(tool_identity)
    if text_stream:
        payload["text_stream"] = text_stream
    for key, value in identity.items():
        if key in {"node_id", "nodeId"} and _subject_node_id(subject):
            continue
        if text(value):
            payload[key] = value
    _apply_subject_node_identity(payload, subject)
    if source_seq > 0:
        payload["source_seq"] = source_seq
        payload["sourceSeq"] = source_seq
    if mission_seq > 0:
        payload["seq"] = mission_seq
        payload["team_mission_event_seq"] = mission_seq
        payload["teamMissionEventSeq"] = mission_seq
    for key in ("run_id", "runId", "turn_id", "turnId"):
        value = source_event.get(key)
        if text(value):
            payload[key] = value
    event: Dict[str, Any] = {
        "type": TEAM_MISSION_RUNTIME_EVENT_TYPE,
        "seq": mission_seq or source_seq,
        "source_seq": source_seq,
        "team_mission_event_seq": mission_seq or source_seq,
        "timestamp": timestamp,
        "payload": payload,
    }
    if subject:
        event["subject"] = subject
    if text_stream:
        event["text_stream"] = text_stream
    for key, value in tool_identity.items():
        if text(value):
            event[key] = value
    for key, value in identity.items():
        if key in {"node_id", "nodeId"} and _subject_node_id(subject):
            continue
        if text(value):
            event[key] = value
    _apply_subject_node_identity(event, subject)
    for key in ("run_id", "turn_id", "session_id", "stored_session_id", "runtime_session_id", "runtime_scope_key"):
        value = source_event.get(key)
        if text(value):
            event[key] = value
    return event


def _with_mission_seq(event: Dict[str, Any], seq: int) -> Dict[str, Any]:
    patched = dict(event or {})
    payload = dict(event_payload(patched))
    patched["seq"] = seq
    patched["team_mission_event_seq"] = seq
    payload["seq"] = seq
    payload["team_mission_event_seq"] = seq
    payload["teamMissionEventSeq"] = seq
    patched["payload"] = payload
    return patched


def runtime_dedupe_key(mission_id: str, run_id: str, source_event: Dict[str, Any]) -> str:
    stream_delta_key = runtime_stream_delta_dedupe_key(mission_id, run_id, source_event)
    if stream_delta_key:
        return stream_delta_key
    return ":".join(
        [
            "runtime",
            text(mission_id),
            text(run_id) or text(source_event.get("run_id") or source_event.get("runId")),
            text(source_event.get("stored_session_id") or source_event.get("session_id")),
            source_event_type(source_event),
            str(event_seq(source_event)),
        ]
    )


def runtime_stream_delta_dedupe_key(mission_id: str, run_id: str, source_event: Dict[str, Any]) -> str:
    event_type = source_event_type(source_event)
    if event_type != "message.delta":
        return ""
    payload = event_payload(source_event)
    mode = text(payload.get("mode")).lower()
    if mode not in {"", "append"}:
        return ""
    offset = _payload_int(payload, "offset")
    if offset is None:
        return ""
    fragment = _payload_stream_fragment(payload)
    if fragment == "":
        return ""
    stream_id = _first_text(
        payload.get("stream_id"),
        payload.get("streamId"),
        payload.get("client_message_id"),
        payload.get("clientMessageId"),
    )
    fragment_hash = hashlib.sha256(fragment.encode("utf-8")).hexdigest()[:20]
    return ":".join(
        [
            "runtime-stream-delta",
            text(mission_id),
            text(run_id) or text(source_event.get("run_id") or source_event.get("runId")),
            text(source_event.get("stored_session_id") or source_event.get("session_id")),
            event_type,
            stream_id,
            str(offset),
            fragment_hash,
        ]
    )


def status_dedupe_key(mission_id: str, source_mission_seq: int, source_event: Dict[str, Any]) -> str:
    return ":".join(
        [
            "conversation-status",
            text(mission_id),
            str(int(source_mission_seq or 0)),
            source_event_type(source_event),
        ]
    )


def structural_dedupe_key(mission_id: str, source_event: Dict[str, Any]) -> str:
    event_type = source_event_type(source_event)
    payload = event_payload(source_event)
    node = _payload_node(payload)
    edge = _payload_edge(payload)
    entity_id = ""
    if node:
        entity_id = _first_text(node.get("node_id"), node.get("nodeId"), node.get("id"))
    if not entity_id and edge:
        from_node_id = _first_text(edge.get("from_node_id"), edge.get("fromNodeId"), edge.get("source"), edge.get("from"))
        to_node_id = _first_text(edge.get("to_node_id"), edge.get("toNodeId"), edge.get("target"), edge.get("to"))
        entity_id = _first_text(
            edge.get("edge_id"),
            edge.get("edgeId"),
            edge.get("id"),
            f"{from_node_id}->{to_node_id}:{_first_text(edge.get('kind'), payload.get('kind'), 'depends_on')}"
            if from_node_id and to_node_id
            else "",
        )
    if not entity_id:
        entity_id = str(event_seq(source_event) or "")
    if not entity_id:
        entity_id = json_dumps(payload)
    return ":".join(["structural", text(mission_id), event_type, entity_id])


def _row_to_event(row: sqlite3.Row | None) -> Dict[str, Any]:
    if row is None:
        return {}
    event = json_loads(row["event_json"], {})
    return event if isinstance(event, dict) else {}


def append_team_mission_event(
    db: Any,
    *,
    mission_id: str,
    event: Dict[str, Any],
    dedupe_key: str,
    source_event: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Append a Team Mission audit event.

    Audit log only; not for timeline render. User-visible conversation
    timelines are rendered from run_events for the stored conversation.
    """
    mission_id = text(mission_id)
    dedupe_key = text(dedupe_key)
    if not mission_id or not dedupe_key or not isinstance(event, dict):
        return {}
    source_event = source_event if isinstance(source_event, dict) else {}
    now = time.time()
    payload = event_payload(event)
    event_type = text(event.get("type"))
    source_type = text(payload.get("source_event_type") or payload.get("sourceEventType") or source_event_type(source_event))
    source_run_id = text(event.get("run_id") or payload.get("run_id") or payload.get("runId") or source_event.get("run_id"))
    source_session_id = text(
        event.get("stored_session_id")
        or payload.get("stable_session_id")
        or source_event.get("stored_session_id")
        or source_event.get("session_id")
    )
    source_seq = int(payload.get("source_seq") or payload.get("sourceSeq") or event_seq(source_event) or 0)
    inserted = False
    with db._lock:
        existing = db._conn.execute(
            "SELECT event_json FROM team_mission_events WHERE mission_id = ? AND dedupe_key = ?",
            (mission_id, dedupe_key),
        ).fetchone()
        if existing is not None:
            duplicate = _row_to_event(existing)
            duplicate["_persistence_disposition"] = "duplicate_mission_event"
            return duplicate
        row = db._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM team_mission_events WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        seq = int((row["next_seq"] if row is not None else 1) or 1)
        stored = _with_mission_seq(event, seq)
        try:
            db._conn.execute(
                """
                INSERT INTO team_mission_events (
                    mission_id, seq, event_type, source_event_type,
                    source_run_id, source_session_id, source_seq, dedupe_key,
                    timestamp, payload_json, source_event_json, event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mission_id,
                    seq,
                    event_type,
                    source_type,
                    source_run_id,
                    source_session_id,
                    source_seq,
                    dedupe_key,
                    float(stored.get("timestamp") or now),
                    "",
                    "",
                    json_dumps(stored),
                    now,
                ),
            )
            inserted = True
        except sqlite3.IntegrityError:
            existing = db._conn.execute(
                "SELECT event_json FROM team_mission_events WHERE mission_id = ? AND dedupe_key = ?",
                (mission_id, dedupe_key),
            ).fetchone()
            duplicate = _row_to_event(existing)
            duplicate["_persistence_disposition"] = "duplicate_mission_event"
            return duplicate
    if inserted:
        notify_team_mission_event_listeners(mission_id, stored)
    return stored


def append_team_mission_runtime_event(
    db: Any,
    *,
    mission_id: str,
    run_id: str,
    source_event: Dict[str, Any],
    identity: Dict[str, str],
) -> Dict[str, Any]:
    if _is_snapshot_message_delta(source_event):
        _emit_team_event_log_diagnostic(
            "runtime-event-skip-snapshot-delta",
            mission_id=mission_id,
            run_id=run_id,
            source_seq=event_seq(source_event),
            **_text_stream_summary(source_event),
        )
        return {}
    source_seq = event_seq(source_event)
    projected = projection_event(source_event, identity, source_seq=source_seq)
    stored = append_team_mission_event(
        db,
        mission_id=mission_id,
        event=projected,
        dedupe_key=runtime_dedupe_key(mission_id, run_id, source_event),
        source_event=source_event,
    )
    payload = event_payload(stored)
    subject = mapping(payload.get("subject"))
    text_stream = mapping(payload.get("text_stream"))
    _emit_team_event_log_diagnostic(
        "runtime-event-appended",
        mission_id=mission_id,
        run_id=run_id,
        source_seq=source_seq,
        stored_seq=stored.get("seq"),
        disposition=stored.get("_persistence_disposition", "inserted"),
        kind=stored.get("kind") or payload.get("kind"),
        source_event_type=payload.get("source_event_type") or payload.get("sourceEventType"),
        subject_type=subject.get("type"),
        subject_id=subject.get("id"),
        subject_node_id=subject.get("node_id") or subject.get("nodeId"),
        runtime_stable_session_id=subject.get("runtime_stable_session_id") or subject.get("runtimeStableSessionId"),
        text_event=text_stream.get("event"),
        text_len=len(raw_text(text_stream.get("delta") or text_stream.get("text"))),
    )
    return stored


def append_team_mission_structural_event(
    db: Any,
    *,
    mission_id: str,
    source_event: Dict[str, Any],
    identity: Dict[str, str] | None = None,
    dedupe_key: str = "",
) -> Dict[str, Any]:
    mission_id = text(mission_id)
    if not mission_id or not isinstance(source_event, dict):
        return {}
    source_event = dict(source_event)
    payload = event_payload(source_event)
    node = _payload_node(payload)
    edge = _payload_edge(payload)
    event_type = source_event_type(source_event)
    event_identity: Dict[str, str] = dict(identity or {})
    event_identity.setdefault("mission_id", mission_id)
    event_identity.setdefault("missionId", mission_id)
    if node:
        node_id = _first_text(node.get("node_id"), node.get("nodeId"), node.get("id"))
        if node_id:
            event_identity.setdefault("node_id", node_id)
            event_identity.setdefault("nodeId", node_id)
        metadata = mapping(node.get("metadata"))
        task_id = _first_text(metadata.get("task_id"), metadata.get("taskId"), payload.get("task_id"), payload.get("taskId"))
        if task_id:
            event_identity.setdefault("task_id", task_id)
            event_identity.setdefault("taskId", task_id)
    elif event_type == "mission.edge.created" and edge:
        from_node_id = _first_text(edge.get("from_node_id"), edge.get("fromNodeId"), edge.get("source"), edge.get("from"))
        to_node_id = _first_text(edge.get("to_node_id"), edge.get("toNodeId"), edge.get("target"), edge.get("to"))
        edge_id = _first_text(
            edge.get("edge_id"),
            edge.get("edgeId"),
            edge.get("id"),
            f"{from_node_id}->{to_node_id}" if from_node_id and to_node_id else "",
        )
        if edge_id:
            event_identity.setdefault("edge_id", edge_id)
            event_identity.setdefault("edgeId", edge_id)
    return append_team_mission_event(
        db,
        mission_id=mission_id,
        event=projection_event(source_event, event_identity),
        dedupe_key=text(dedupe_key) or structural_dedupe_key(mission_id, source_event),
        source_event=source_event,
    )


def append_team_mission_conversation_status_event(
    db: Any,
    *,
    mission_id: str,
    source_event: Dict[str, Any],
    source_mission_seq: int,
) -> Dict[str, Any]:
    status_event_builder = getattr(db, "_team_mission_conversation_status_event", None)
    if not callable(status_event_builder):
        return {}
    projection_seq = int(source_mission_seq or 0) + 1
    event = status_event_builder(
        mission_id=mission_id,
        source_event=source_event,
        source_seq=int(source_mission_seq or 0),
        projection_seq=projection_seq,
    )
    if not event:
        return {}
    payload = dict(event_payload(event))
    payload["protocol"] = TEAM_MISSION_EVENT_PROTOCOL
    payload["kind"] = "conversation.status.updated"
    payload["source_event_seq"] = int(source_mission_seq or 0)
    payload["sourceEventSeq"] = int(source_mission_seq or 0)
    event["payload"] = payload
    return append_team_mission_event(
        db,
        mission_id=mission_id,
        event=event,
        dedupe_key=status_dedupe_key(mission_id, source_mission_seq, source_event),
        source_event=source_event,
    )


def append_team_mission_event_for_run(
    db: Any,
    *,
    run_id: str,
    event: Dict[str, Any],
) -> Dict[str, Any]:
    binding_getter = getattr(db, "get_team_mission_run_binding", None)
    if not callable(binding_getter):
        _emit_team_event_log_diagnostic(
            "runtime-event-drop-no-binding-getter",
            run_id=run_id,
            **_text_stream_summary(event),
        )
        return {}
    binding = binding_getter(run_id)
    if not isinstance(binding, dict) or not binding:
        _emit_team_event_log_diagnostic(
            "runtime-event-drop-no-binding",
            run_id=run_id,
            **_text_stream_summary(event),
        )
        return {}
    mission_id = text(binding.get("mission_id"))
    node = {}
    node_getter = getattr(db, "get_team_mission_node", None)
    if callable(node_getter):
        try:
            node = node_getter(mission_id, text(binding.get("node_id"))) or {}
        except Exception:
            node = {}
    graph_getter = getattr(db, "get_team_mission_graph", None)
    mission = {"mission_id": mission_id}
    if callable(graph_getter):
        try:
            graph = graph_getter(mission_id)
            if isinstance(graph, dict) and isinstance(graph.get("mission"), dict):
                mission = graph["mission"]
        except Exception:
            mission = {"mission_id": mission_id}
    identity_builder = getattr(db, "_team_mission_runtime_event_identity", None)
    if not callable(identity_builder):
        _emit_team_event_log_diagnostic(
            "runtime-event-drop-no-identity-builder",
            mission_id=mission_id,
            run_id=run_id,
            node_id=text(binding.get("node_id")),
            **_text_stream_summary(event),
        )
        return {}
    identity = identity_builder(mission=mission, node=node, binding=binding)
    _emit_team_event_log_diagnostic(
        "runtime-event-project-start",
        mission_id=mission_id,
        run_id=run_id,
        node_id=text(binding.get("node_id")),
        binding_session_id=text(binding.get("session_id")),
        binding_runtime_scope_key=text(binding.get("runtime_scope_key")),
        identity=identity,
        **_text_stream_summary(event),
    )
    return append_team_mission_runtime_event(
        db,
        mission_id=mission_id,
        run_id=run_id,
        source_event=event,
        identity=identity,
    )


def list_team_mission_events(
    db: Any,
    mission_id: str,
    *,
    after_seq: int = 0,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    """List Team Mission audit events.

    Audit log only; not for timeline render.
    """
    mission_id = text(mission_id)
    if not mission_id:
        return []
    after_seq = int(after_seq or 0)
    safe_limit = max(1, min(int(limit or 2000), 10000))
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT event_json
            FROM team_mission_events
            WHERE mission_id = ?
              AND seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (mission_id, after_seq, safe_limit),
        ).fetchall()
    events: List[Dict[str, Any]] = []
    for row in rows:
        event = _row_to_event(row)
        if event:
            events.append(event)
    return events
