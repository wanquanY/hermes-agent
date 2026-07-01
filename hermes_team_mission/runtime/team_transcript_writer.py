from __future__ import annotations

import json
import time
from pathlib import PurePath
from typing import Any

from hermes_conversation_message_identity import AssistantMessageIdentity
from hermes_conversation_message_identity import assistant_conversation_message_id_for
from hermes_conversation_message_identity import user_conversation_message_id_for
from hermes_runtime_event_payloads import primary_deliverable_text
from hermes_team_mission.context.artifact_refs import artifact_refs_from_event
from hermes_team_mission.context.artifact_refs import dedupe_artifact_refs
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind


MAIN_TRANSCRIPT_ACTIVITY_KINDS = frozenset({
    "leader_chat",
    "member_direct_chat",
    "team_dispatch",
    "mission_start",
    "mission_summary",
    "mission_report",
})

MISSION_NODE_TRANSCRIPT_ACTIVITY_KIND = "mission_node"
TERMINAL_MISSION_SUMMARY_OUTCOMES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "interrupted",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _artifact_display_name(ref: dict[str, Any]) -> str:
    title = _text(ref.get("title") or ref.get("name") or ref.get("filename") or ref.get("fileName"))
    if title:
        return title
    path = _text(ref.get("path") or ref.get("uri") or ref.get("url"))
    if path:
        try:
            return PurePath(path).name or path
        except Exception:
            return path.rsplit("/", 1)[-1] or path
    return _text(ref.get("id") or ref.get("artifact_id") or ref.get("artifactId")) or "交付文件"


def _artifact_card(ref: dict[str, Any]) -> dict[str, Any]:
    path = _text(ref.get("path") or ref.get("file_path") or ref.get("filePath") or ref.get("uri") or ref.get("url"))
    title = _artifact_display_name(ref)
    artifact_id = _text(ref.get("id") or ref.get("artifact_id") or ref.get("artifactId") or path or title)
    card = {
        **ref,
        "id": artifact_id,
        "title": title,
    }
    if path:
        card["path"] = path
    return card


def _mission_report_artifact_cards(artifact_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards = []
    for ref in artifact_refs:
        card = _artifact_card(ref)
        if _text(card.get("path")):
            cards.append(card)
    return dedupe_artifact_refs(cards)


def _emit_transcript_writer_diagnostic(stage: str, **fields: Any) -> None:
    try:
        from agent.dovie_diagnostics import emit_dovie_diagnostic

        emit_dovie_diagnostic("[dovie-team-transcript-writer-debug]", {"stage": stage, **fields})
    except Exception:
        pass


def _message_diagnostic_summary(message: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(message.get("metadata"))
    team = _mapping(metadata.get("team_mission") or metadata.get("teamMission"))
    content = _text(message.get("text") or message.get("content"))
    return {
        "message_id": _text(message.get("message_id") or message.get("messageId") or message.get("id")),
        "conversation_message_id": _text(
            message.get("conversation_message_id") or message.get("conversationMessageId")
        ),
        "role": _text(message.get("role")),
        "participant_id": _text(message.get("participant_id") or message.get("participantId")),
        "activity_kind": _text(metadata.get("activity_kind") or metadata.get("activityKind")),
        "transcript_activity_kind": _text(
            metadata.get("transcript_activity_kind") or metadata.get("transcriptActivityKind")
        ),
        "team_mission_kind": _text(team.get("kind")),
        "source_node_id": _text(team.get("source_node_id") or team.get("sourceNodeId")),
        "text_len": len(content),
        "text_preview": content[:80].replace("\n", "\\n"),
    }


def normalized_mission_summary_outcome(value: Any) -> str:
    outcome = _text(value).lower()
    if outcome == "canceled":
        return "cancelled"
    return outcome


def transcript_activity_kind_for_run_context(
    *,
    activity_kind: str,
    activity_id: str = "",
) -> str:
    kind = _text(activity_kind)
    activity = _text(activity_id)
    if activity.startswith("chat:team-session-team-conversation-"):
        return "leader_chat"
    if activity.startswith("team-conversation:"):
        return "leader_chat"
    if activity.startswith("act-member_chat:"):
        return "member_direct_chat"
    if activity.startswith("act-team_dispatch"):
        return "team_dispatch"
    if kind == "member_chat":
        return "member_direct_chat"
    if kind == "chat":
        return "leader_chat"
    if kind == "mission":
        if activity.startswith("act-node:"):
            return MISSION_NODE_TRANSCRIPT_ACTIVITY_KIND
        return "mission_start"
    if kind == "team_dispatch":
        return "team_dispatch"
    return kind


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    return _mapping(event.get("payload"))


def _event_activity_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
    run_context = _mapping(payload.get("run_context") or payload.get("runContext"))
    return _text(
        event.get("activity_id")
        or event.get("activityId")
        or payload.get("activity_id")
        or payload.get("activityId")
        or run_context.get("activity_id")
        or run_context.get("activityId")
    )


def _event_transcript_activity_kind(event: dict[str, Any], payload: dict[str, Any]) -> str:
    run_context = _mapping(payload.get("run_context") or payload.get("runContext"))
    explicit = _text(
        event.get("transcript_activity_kind")
        or event.get("transcriptActivityKind")
        or payload.get("transcript_activity_kind")
        or payload.get("transcriptActivityKind")
    )
    if explicit:
        return explicit
    return transcript_activity_kind_for_run_context(
        activity_kind=_text(
            event.get("activity_kind")
            or event.get("activityKind")
            or payload.get("activity_kind")
            or payload.get("activityKind")
            or run_context.get("activity_kind")
            or run_context.get("activityKind")
        ),
        activity_id=_event_activity_id(event, payload),
    )


def _event_conversation_session_id(event: dict[str, Any], payload: dict[str, Any], fallback: str) -> str:
    run_context = _mapping(payload.get("run_context") or payload.get("runContext"))
    return _text(
        event.get("conversation_session_id")
        or event.get("conversationSessionId")
        or payload.get("conversation_session_id")
        or payload.get("conversationSessionId")
        or run_context.get("conversation_session_id")
        or run_context.get("conversationSessionId")
        or event.get("stored_session_id")
        or payload.get("stored_session_id")
        or payload.get("storedSessionId")
        or fallback
    )


def _event_text(event: dict[str, Any], payload: dict[str, Any]) -> str:
    return _text(
        primary_deliverable_text(payload)
        or payload.get("text")
        or payload.get("content")
        or event.get("text")
    )


def _event_participant_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
    return _text(
        event.get("participant_id")
        or event.get("participantId")
        or payload.get("participant_id")
        or payload.get("participantId")
    )


def _event_run_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
    return _text(event.get("run_id") or event.get("runId") or payload.get("run_id") or payload.get("runId"))


def _event_turn_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
    return _text(event.get("turn_id") or event.get("turnId") or payload.get("turn_id") or payload.get("turnId"))


def _event_message_seq(event: dict[str, Any], payload: dict[str, Any]) -> str:
    return _text(
        event.get("message_seq_in_run")
        or event.get("messageSeqInRun")
        or payload.get("message_seq_in_run")
        or payload.get("messageSeqInRun")
        or payload.get("client_message_id")
        or payload.get("clientMessageId")
        or event.get("seq")
    )


def main_transcript_activity_kind(message: dict[str, Any]) -> str:
    metadata = _mapping(message.get("metadata"))
    return _text(
        metadata.get("transcript_activity_kind")
        or metadata.get("transcriptActivityKind")
        or metadata.get("activity_kind")
        or metadata.get("activityKind")
    )


def main_transcript_message_decision(message: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(message.get("metadata"))
    kind = main_transcript_activity_kind(message)
    if kind:
        include = kind in MAIN_TRANSCRIPT_ACTIVITY_KINDS
        return {
            "include": include,
            "reason": "activity_kind_allowlist" if include else "activity_kind_excluded",
            "transcript_activity_kind": kind,
            "activity_kind": _text(metadata.get("activity_kind") or metadata.get("activityKind")),
        }
    activity_id = _text(metadata.get("activity_id") or metadata.get("activityId"))
    if activity_id.startswith("act-node:"):
        return {
            "include": False,
            "reason": "activity_id_node",
            "transcript_activity_kind": MISSION_NODE_TRANSCRIPT_ACTIVITY_KIND,
            "activity_kind": _text(metadata.get("activity_kind") or metadata.get("activityKind")),
        }
    team = _mapping(metadata.get("team_mission") or metadata.get("teamMission"))
    if team.get("team_mission_conversation_mirror") or metadata.get("team_mission_conversation_mirror"):
        return {
            "include": False,
            "reason": "legacy_conversation_mirror",
            "transcript_activity_kind": "",
            "activity_kind": _text(metadata.get("activity_kind") or metadata.get("activityKind")),
        }
    # Legacy pre-allowlist rows are preserved in main render. They are audited
    # separately so the read path does not guess and hide user-visible history.
    return {
        "include": True,
        "reason": "legacy_no_activity_kind",
        "transcript_activity_kind": "",
        "activity_kind": _text(metadata.get("activity_kind") or metadata.get("activityKind")),
    }


def is_main_transcript_message(message: dict[str, Any]) -> bool:
    return bool(main_transcript_message_decision(message).get("include"))


def is_node_transcript_message(message: dict[str, Any]) -> bool:
    decision = main_transcript_message_decision(message)
    return not bool(decision.get("include"))


def _upsert_team_message_by_id(
    db: Any,
    *,
    session_id: str,
    conversation_message_id: str,
    role: str,
    content: Any,
    participant_id: str = "",
    metadata: dict[str, Any] | None = None,
    status: str = "",
    reasoning: Any = "",
) -> dict[str, Any]:
    upsert = getattr(db, "_upsert_team_message_by_id", None)
    if not callable(upsert):
        raise RuntimeError("SessionDB does not support team transcript message upsert")
    return upsert(
        session_id=session_id,
        conversation_message_id=conversation_message_id,
        role=role,
        content=content,
        participant_id=participant_id,
        metadata=dict(metadata or {}),
        status=status,
        reasoning=reasoning,
    )


def _mission_report_context_for_run(db: Any, run_id: str, *, conn: Any | None = None) -> dict[str, Any]:
    run_id = _text(run_id)
    if not run_id:
        return {}
    binding: dict[str, Any] = {}
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            binding = {
                "mission_id": _text(_row_value(row, "mission_id")),
                "node_id": _text(_row_value(row, "node_id")),
                "run_id": _text(_row_value(row, "run_id")),
                "session_id": _text(_row_value(row, "session_id")),
                "runtime_session_id": _text(_row_value(row, "runtime_session_id")),
                "runtime_scope_key": _text(_row_value(row, "runtime_scope_key")),
                "role": _text(_row_value(row, "role")),
                "metadata": _json_loads(_row_value(row, "metadata_json"), {}),
            }
    else:
        binding_getter = getattr(db, "get_team_mission_run_binding", None)
        if callable(binding_getter):
            try:
                binding = _mapping(binding_getter(run_id))
            except Exception:
                binding = {}
    metadata = _mapping(binding.get("metadata"))
    if _text(metadata.get("kind") or metadata.get("team_mission_kind")) != "leader_report":
        return {}
    mission_id = _text(binding.get("mission_id") or metadata.get("mission_id") or metadata.get("missionId"))
    if not mission_id:
        return {}
    result: dict[str, Any] = {}
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT * FROM team_mission_results WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            artifact_refs_json = _json_loads(_row_value(row, "artifact_refs_json"), [])
            node_results_json = _json_loads(_row_value(row, "node_results_json"), [])
            result = {
                "result_id": _text(_row_value(row, "result_id")),
                "resultId": _text(_row_value(row, "result_id")),
                "mission_id": _text(_row_value(row, "mission_id")),
                "missionId": _text(_row_value(row, "mission_id")),
                "activity_id": _text(_row_value(row, "activity_id")),
                "activityId": _text(_row_value(row, "activity_id")),
                "status": _text(_row_value(row, "status")),
                "outcome": _text(_row_value(row, "outcome")),
                "summary_text": _text(_row_value(row, "summary_text")),
                "summaryText": _text(_row_value(row, "summary_text")),
                "node_results": node_results_json if isinstance(node_results_json, list) else [],
                "nodeResults": node_results_json if isinstance(node_results_json, list) else [],
                "artifact_refs": artifact_refs_json if isinstance(artifact_refs_json, list) else [],
                "artifactRefs": artifact_refs_json if isinstance(artifact_refs_json, list) else [],
                "leader_report_run_id": _text(_row_value(row, "leader_report_run_id")),
                "leaderReportRunId": _text(_row_value(row, "leader_report_run_id")),
                "leader_report_message_id": _text(_row_value(row, "leader_report_message_id")),
                "leaderReportMessageId": _text(_row_value(row, "leader_report_message_id")),
                "metadata": _json_loads(_row_value(row, "metadata_json"), {}),
            }
    else:
        result_getter = getattr(db, "get_team_mission_result", None)
        if callable(result_getter):
            try:
                result = _mapping(result_getter(mission_id))
            except Exception:
                result = {}
    mission: dict[str, Any] = {}
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            mission = {
                "mission_id": _text(_row_value(row, "mission_id")),
                "conversation_id": _text(_row_value(row, "conversation_id")),
                "team_id": _text(_row_value(row, "team_id")),
                "title": _text(_row_value(row, "title")),
                "objective": _text(_row_value(row, "objective")),
                "status": _text(_row_value(row, "status")),
                "metadata": _json_loads(_row_value(row, "metadata_json"), {}),
            }
    artifact_refs = [
        dict(item)
        for item in (
            (result.get("artifact_refs") or result.get("artifactRefs"))
            or metadata.get("artifact_refs")
            or metadata.get("artifactRefs")
            or []
        )
        if isinstance(item, dict)
    ]
    return {
        "binding": binding,
        "metadata": metadata,
        "mission": mission,
        "mission_id": mission_id,
        "result": result,
        "result_id": _text(result.get("result_id") or result.get("resultId") or metadata.get("result_id")),
        "outcome": _text(result.get("outcome") or metadata.get("outcome")),
        "source_node_id": _text(metadata.get("source_node_id") or metadata.get("sourceNodeId")),
        "artifact_cards": _mission_report_artifact_cards(artifact_refs),
    }


def _update_leader_report_message_id_locked(
    conn: Any,
    *,
    mission_id: str,
    run_id: str,
    conversation_message_id: str,
) -> bool:
    mission_id = _text(mission_id)
    conversation_message_id = _text(conversation_message_id)
    if not mission_id or not conversation_message_id:
        return False
    try:
        cursor = conn.execute(
            """
            UPDATE team_mission_results
               SET leader_report_run_id = COALESCE(NULLIF(leader_report_run_id, ''), ?),
                   leader_report_message_id = ?,
                   updated_at = ?
             WHERE mission_id = ?
            """,
            (_text(run_id), conversation_message_id, time.time(), mission_id),
        )
        return bool(getattr(cursor, "rowcount", 0))
    except Exception:
        return False


def _append_leader_report_ready_event(
    db: Any,
    *,
    mission_id: str,
    run_id: str,
    conversation_message_id: str,
) -> None:
    mission_id = _text(mission_id)
    run_id = _text(run_id)
    conversation_message_id = _text(conversation_message_id)
    if not mission_id or not conversation_message_id:
        return
    source_event = {
        "type": "mission.report.ready",
        "timestamp": time.time(),
        "run_id": run_id,
        "payload": {
            "mission_id": mission_id,
            "missionId": mission_id,
            "leader_report_run_id": run_id,
            "leaderReportRunId": run_id,
            "leader_report_message_id": conversation_message_id,
            "leaderReportMessageId": conversation_message_id,
            "status": "ready",
        },
    }
    append_structural = getattr(db, "append_team_mission_structural_event", None)
    append_status = getattr(db, "append_team_mission_conversation_status_event", None)
    if not callable(append_structural) or not callable(append_status):
        return
    try:
        stored = append_structural(
            mission_id=mission_id,
            source_event=source_event,
            identity={"mission_id": mission_id, "missionId": mission_id},
            dedupe_key=f"mission-report-ready:{mission_id}:{conversation_message_id}",
        )
        try:
            source_seq = int((stored or {}).get("seq") or (stored or {}).get("team_mission_event_seq") or 0)
        except (TypeError, ValueError):
            source_seq = 0
        if source_seq > 0:
            append_status(
                mission_id=mission_id,
                source_event=source_event,
                source_mission_seq=source_seq,
            )
    except Exception:
        pass


def leader_report_ready_context_for_run(
    db: Any,
    *,
    run_id: str,
    projected_message_id: str = "",
) -> dict[str, Any]:
    run_id = _text(run_id)
    if not run_id:
        return {}
    binding_getter = getattr(db, "get_team_mission_run_binding", None)
    result_getter = getattr(db, "get_team_mission_result", None)
    if not callable(binding_getter) or not callable(result_getter):
        return {}
    try:
        binding = _mapping(binding_getter(run_id))
    except Exception:
        binding = {}
    metadata = _mapping(binding.get("metadata"))
    if _text(metadata.get("kind") or metadata.get("team_mission_kind")) != "leader_report":
        return {}
    mission_id = _text(binding.get("mission_id") or metadata.get("mission_id") or metadata.get("missionId"))
    if not mission_id:
        return {}
    try:
        result = _mapping(result_getter(mission_id))
    except Exception:
        result = {}
    conversation_message_id = _text(
        projected_message_id
        or result.get("leader_report_message_id")
        or result.get("leaderReportMessageId")
    )
    if not conversation_message_id:
        return {}
    return {
        "mission_id": mission_id,
        "missionId": mission_id,
        "run_id": run_id,
        "runId": run_id,
        "leader_report_message_id": conversation_message_id,
        "leaderReportMessageId": conversation_message_id,
    }


class UserSubmissionWriter:
    @staticmethod
    def write_user_submission(
        db: Any,
        *,
        conversation_id: str,
        conversation_session_id: str,
        run_id: str,
        turn_id: str,
        text: str,
        target_member_id: str = "",
        display_name: str = "",
        client_message_id: str = "",
        source_kind: str,
        transcript_activity_kind: str = "",
    ) -> dict[str, Any]:
        conversation_message_id = user_conversation_message_id_for(
            session_id=conversation_session_id,
            turn_id=turn_id,
            run_id=run_id,
            client_message_id=client_message_id,
        )
        inferred_kind = (
            _text(transcript_activity_kind)
            or ("member_direct_chat" if source_kind == "member_chat_user" else "leader_chat")
        )
        team_metadata = {
            "kind": source_kind,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
        }
        if target_member_id:
            team_metadata["target_member_id"] = target_member_id
        if display_name:
            team_metadata["display_name"] = display_name
        metadata = {
            "source": "team_mission.message.submit",
            "message_kind": "user_submission",
            "run_id": run_id,
            "turn_id": turn_id,
            "activity_kind": inferred_kind,
            "transcript_activity_kind": inferred_kind,
            "team_mission": team_metadata,
        }
        if client_message_id:
            metadata["client_message_id"] = client_message_id
        saved = _upsert_team_message_by_id(
            db,
            session_id=conversation_session_id,
            conversation_message_id=conversation_message_id,
            role="user",
            content=text,
            participant_id="",
            metadata=metadata,
            status="completed",
        )
        _emit_transcript_writer_diagnostic(
            "user-submission-written",
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            turn_id=turn_id,
            source_kind=source_kind,
            target_member_id=target_member_id,
            decision=main_transcript_message_decision(saved),
            message=_message_diagnostic_summary(saved),
        )
        return saved


class RuntimeTranscriptWriter:
    @staticmethod
    def project_message_complete_event_locked(
        db: Any,
        conn: Any,
        *,
        session_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(event, dict) or _text(event.get("type")) != "message.complete":
            return {}
        payload = _event_payload(event)
        run_id = _event_run_id(event, payload)
        report_context = _mission_report_context_for_run(db, run_id, conn=conn)
        transcript_activity_kind = "mission_report" if report_context else _event_transcript_activity_kind(event, payload)
        if transcript_activity_kind not in MAIN_TRANSCRIPT_ACTIVITY_KINDS:
            return {}
        conversation_session_id = _event_conversation_session_id(event, payload, session_id)
        if not conversation_session_id or conversation_session_id.startswith("team:mission:"):
            return {}
        text = _event_text(event, payload)
        if not text:
            return {}
        message_seq = _event_message_seq(event, payload)
        if not run_id or not message_seq:
            return {}
        turn_id = _event_turn_id(event, payload)
        conversation_message_id = assistant_conversation_message_id_for(
            AssistantMessageIdentity(
                session_id=conversation_session_id,
                run_id=run_id,
                message_seq_in_run=message_seq,
            )
        )
        activity_id = _event_activity_id(event, payload)
        participant_id = _event_participant_id(event, payload)
        run_context = _mapping(payload.get("run_context") or payload.get("runContext"))
        client_message_id = _text(payload.get("client_message_id") or payload.get("clientMessageId"))
        team_metadata = {
            "kind": transcript_activity_kind,
            "conversation_session_id": conversation_session_id,
        }
        if activity_id:
            team_metadata["activity_id"] = activity_id
        artifact_cards: list[dict[str, Any]] = []
        if report_context:
            mission_id = _text(report_context.get("mission_id"))
            result_id = _text(report_context.get("result_id"))
            source_node_id = _text(report_context.get("source_node_id"))
            outcome = _text(report_context.get("outcome"))
            artifact_cards = [
                dict(item)
                for item in report_context.get("artifact_cards") or []
                if isinstance(item, dict)
            ]
            team_metadata.update({
                "kind": "mission_report",
                "mission_id": mission_id,
                "missionId": mission_id,
                "result_id": result_id,
                "resultId": result_id,
                "source_node_id": source_node_id,
                "sourceNodeId": source_node_id,
                "outcome": outcome,
            })
            if artifact_cards:
                team_metadata["artifact_refs"] = artifact_cards
                team_metadata["artifactRefs"] = artifact_cards
            if not activity_id and mission_id:
                activity_id = f"mission-report:{mission_id}"
                team_metadata["activity_id"] = activity_id
        metadata = {
            "source": "team_mission.runtime_event",
            "message_kind": "assistant_reply",
            "run_id": run_id,
            "turn_id": turn_id,
            "activity_kind": transcript_activity_kind,
            "transcript_activity_kind": transcript_activity_kind,
            "team_mission": team_metadata,
        }
        if report_context:
            metadata["kind"] = "mission_report"
            metadata["source"] = "team_mission.leader_report_run"
            if artifact_cards:
                metadata["artifacts"] = artifact_cards
        if activity_id:
            metadata["activity_id"] = activity_id
        if client_message_id:
            metadata["client_message_id"] = client_message_id
        if run_context:
            metadata["run_context"] = run_context
        status = _text(payload.get("status")) or "completed"
        upsert_locked = getattr(db, "_upsert_team_message_by_id_locked", None)
        if not callable(upsert_locked):
            return {}
        saved = upsert_locked(
            conn,
            session_id=conversation_session_id,
            conversation_message_id=conversation_message_id,
            role="assistant",
            content=text,
            participant_id=participant_id,
            metadata=metadata,
            status=status,
            reasoning=payload.get("reasoning") or payload.get("reasoning_content") or "",
            timestamp=float(event.get("timestamp") or 0) or None,
        )
        if report_context:
            report_mission_id = _text(report_context.get("mission_id"))
            _update_leader_report_message_id_locked(
                conn,
                mission_id=report_mission_id,
                run_id=run_id,
                conversation_message_id=conversation_message_id,
            )
            if report_mission_id and conversation_message_id:
                saved = dict(saved)
                saved["_team_mission_report_ready"] = {
                    "mission_id": report_mission_id,
                    "missionId": report_mission_id,
                    "run_id": run_id,
                    "runId": run_id,
                    "leader_report_message_id": conversation_message_id,
                    "leaderReportMessageId": conversation_message_id,
                }
        _emit_transcript_writer_diagnostic(
            "runtime-message-complete-projected",
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            turn_id=turn_id,
            activity_id=activity_id,
            participant_id=participant_id,
            conversation_message_id=conversation_message_id,
            decision=main_transcript_message_decision(saved),
            message=_message_diagnostic_summary(saved),
        )
        return saved


def backfill_unprojected_message_complete_events_locked(
    db: Any,
    conn: Any,
    *,
    session_ids: list[str],
    limit: int = 200,
) -> int:
    target_session_ids = [
        _text(session_id)
        for session_id in session_ids
        if _text(session_id)
    ]
    if not target_session_ids:
        return 0
    bounded_limit = max(1, min(int(limit or 200), 1000))
    placeholders = ",".join("?" for _ in target_session_ids)
    try:
        rows = conn.execute(
            f"""
            SELECT id, session_id, event_json
            FROM run_events
            WHERE session_id IN ({placeholders})
              AND event_type = 'message.complete'
              AND COALESCE(projected_message_id, '') = ''
              AND COALESCE(projection_state, '') != 'projected'
            ORDER BY seq
            LIMIT ?
            """,
            (*target_session_ids, bounded_limit),
        ).fetchall()
    except Exception:
        return 0
    repaired = 0
    for row in rows:
        event = _json_loads(_row_value(row, "event_json"), {})
        if not isinstance(event, dict):
            continue
        projected = RuntimeTranscriptWriter.project_message_complete_event_locked(
            db,
            conn,
            session_id=_text(_row_value(row, "session_id")),
            event=event,
        )
        conversation_message_id = _text(
            _mapping(projected).get("conversation_message_id")
            or _mapping(projected).get("conversationMessageId")
        )
        if not conversation_message_id:
            continue
        conn.execute(
            """
            UPDATE run_events
               SET projected_message_id = ?,
                   projection_state = 'projected'
             WHERE id = ?
            """,
            (conversation_message_id, int(_row_value(row, "id") or 0)),
        )
        repaired += 1
    return repaired


def _conversation_session_id_from_mission(mission: dict[str, Any] | None) -> str:
    mission = mission if isinstance(mission, dict) else {}
    metadata = _mapping(mission.get("metadata"))
    return _text(
        metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or metadata.get("stable_team_session_id")
        or metadata.get("stableTeamSessionId")
        or metadata.get("stable_session_id")
        or metadata.get("stableSessionId")
        or mission.get("leader_session_id")
        or mission.get("conversation_session_id")
        or mission.get("stable_session_id")
    )


def _conversation_id_from_mission(mission: dict[str, Any] | None) -> str:
    mission = mission if isinstance(mission, dict) else {}
    metadata = _mapping(mission.get("metadata"))
    return _text(
        mission.get("conversation_id")
        or metadata.get("conversation_id")
        or metadata.get("conversationId")
    )


def _synthesis_node(graph: dict[str, Any]) -> dict[str, Any]:
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if normalize_team_mission_node_kind(node.get("kind")) == "synthesis":
            return node
    return {}


def _payload_stream_text(payload: dict[str, Any]) -> str:
    for key in ("delta", "text", "snapshot"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return ""


def _stream_text_from_events(events: list[dict[str, Any]]) -> str:
    content = ""
    for event in events or []:
        if not isinstance(event, dict) or _text(event.get("type")) != "message.delta":
            continue
        payload = _mapping(event.get("payload"))
        chunk = _payload_stream_text(payload)
        if not chunk:
            continue
        mode = _text(payload.get("mode")).lower()
        if mode == "snapshot" or payload.get("snapshot") is not None:
            content = chunk
        else:
            content += chunk
    return _text(content)


def _summary_payload_from_run_events(
    db: Any,
    *,
    session_id: str,
    run_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    if not session_id or not run_id:
        return "", []
    try:
        events = db.list_run_events(session_id, run_id=run_id, limit=5000) or []
    except Exception:
        return "", []
    artifact_refs: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, dict) and _text(event.get("type")) == "message.complete":
            artifact_refs.extend(artifact_refs_from_event(event))
    for event in reversed(events):
        if not isinstance(event, dict) or _text(event.get("type")) != "message.complete":
            continue
        payload = _mapping(event.get("payload"))
        text = primary_deliverable_text(payload)
        if text:
            return text, dedupe_artifact_refs(artifact_refs)
    return _stream_text_from_events(events), dedupe_artifact_refs(artifact_refs)


def _summary_payload_from_synthesis(
    db: Any,
    graph: dict[str, Any],
    synthesis_node: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    node_id = _text(synthesis_node.get("node_id"))
    if not node_id:
        return "", []
    bindings = [
        item for item in graph.get("run_bindings") or []
        if isinstance(item, dict) and _text(item.get("node_id")) == node_id
    ]
    bindings.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
    for binding in bindings:
        text, artifact_refs = _summary_payload_from_run_events(
            db,
            session_id=_text(binding.get("session_id")),
            run_id=_text(binding.get("run_id")),
        )
        if text or artifact_refs:
            return text, artifact_refs
    return "", []


class MissionSummaryWriter:
    @staticmethod
    def emit_mission_summary(
        db: Any,
        *,
        mission_id: str,
        synthesis_node_id: str = "",
        conversation_session_id: str = "",
        summary_text: str = "",
        outcome: str,
    ) -> dict[str, Any]:
        mission_id = _text(mission_id)
        outcome = normalized_mission_summary_outcome(outcome)
        if not mission_id or outcome not in TERMINAL_MISSION_SUMMARY_OUTCOMES:
            return {}
        if outcome == "canceled":
            outcome = "cancelled"
        conversation_session_id = _text(conversation_session_id)
        graph: dict[str, Any] = {}
        mission: dict[str, Any] = {}
        artifact_refs: list[dict[str, Any]] = []
        if not conversation_session_id or not synthesis_node_id or not summary_text:
            try:
                graph = db.get_team_mission_graph(mission_id) or {}
            except Exception:
                graph = {}
            mission = _mapping(graph.get("mission"))
            conversation_session_id = conversation_session_id or _conversation_session_id_from_mission(mission)
        result: dict[str, Any] = {}
        result_getter = getattr(db, "get_team_mission_result", None)
        if callable(result_getter):
            try:
                result = _mapping(result_getter(mission_id))
            except Exception:
                result = {}
        if result:
            if not _text(summary_text):
                summary_text = _text(result.get("summary_text") or result.get("summaryText"))
            if not artifact_refs:
                artifact_refs = [
                    dict(item)
                    for item in (result.get("artifact_refs") or result.get("artifactRefs") or [])
                    if isinstance(item, dict)
                ]
        synthesis_node: dict[str, Any] = {}
        if graph:
            synthesis_node = _synthesis_node(graph)
            synthesis_node_id = _text(synthesis_node_id) or _text(synthesis_node.get("node_id"))
            if not result and not _text(summary_text):
                summary_text, artifact_refs = _summary_payload_from_synthesis(db, graph, synthesis_node)
        if not conversation_session_id and mission:
            conversation_id = _conversation_id_from_mission(mission)
            if conversation_id:
                try:
                    conversation = db.get_team_mission_conversation(conversation_id) or {}
                except Exception:
                    conversation = {}
                conversation_session_id = _text(conversation.get("stable_session_id"))
        if not conversation_session_id:
            return {}
        summary_text = _text(summary_text)
        if not summary_text:
            summary_text = f"Mission {outcome}: no presentable output."
        if not graph:
            try:
                graph = db.get_team_mission_graph(mission_id) or {}
            except Exception:
                graph = {}
        if not mission:
            mission = _mapping(graph.get("mission"))
        if result:
            artifact_refs = [
                dict(item)
                for item in (result.get("artifact_refs") or result.get("artifactRefs") or artifact_refs)
                if isinstance(item, dict)
            ]
        artifact_cards = _mission_report_artifact_cards(artifact_refs)
        result_id = _text((result or {}).get("result_id") or (result or {}).get("resultId"))
        existing_report_run_id = _text(result.get("leader_report_run_id") or result.get("leaderReportRunId"))
        if existing_report_run_id:
            return {
                "ok": True,
                "status": "already_requested",
                "mission_id": mission_id,
                "missionId": mission_id,
                "result_id": result_id,
                "resultId": result_id,
                "run_id": existing_report_run_id,
                "runId": existing_report_run_id,
                "conversation_session_id": conversation_session_id,
                "conversationSessionId": conversation_session_id,
            }
        try:
            from hermes_team_mission.runtime.leader_report_dispatch import submit_leader_report_run

            submitted = submit_leader_report_run(
                db=db,
                mission_id=mission_id,
                synthesis_node_id=_text(synthesis_node_id),
                conversation_session_id=conversation_session_id,
                outcome=outcome,
                summary_text=summary_text,
                mission=mission,
                result=result,
                artifact_refs=artifact_cards or artifact_refs,
            )
        except Exception as exc:
            submitted = {
                "ok": False,
                "status": "failed",
                "error": str(exc),
                "mission_id": mission_id,
                "missionId": mission_id,
            }
        _emit_transcript_writer_diagnostic(
            "mission-leader-report-requested",
            mission_id=mission_id,
            synthesis_node_id=_text(synthesis_node_id),
            conversation_session_id=conversation_session_id,
            outcome=outcome,
            result_id=result_id,
            submitted_status=_text(submitted.get("status")),
            submitted_ok=bool(submitted.get("ok")),
            submitted_error=_text(submitted.get("error")),
        )
        return submitted
