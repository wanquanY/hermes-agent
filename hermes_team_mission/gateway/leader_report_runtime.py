from __future__ import annotations

from collections.abc import Callable
import json
import os
import uuid
from pathlib import Path
from typing import Any

from hermes_profile_dir import resolve_default_agent_dir
from hermes_state_participants import leader_participant_id
from hermes_team_mission.domain.run_context import RunContext
from hermes_team_mission.gateway.common import _ensure_team_conversation_session
from hermes_team_mission.gateway.common import _ensure_team_mission_runtime_session_shell
from hermes_team_mission.gateway.common import _leader_conversation_runtime_scope_contract_error
from hermes_team_mission.gateway.common import _leader_conversation_runtime_scope_key
from hermes_team_mission.gateway.common import _leader_disabled_toolsets
from hermes_team_mission.gateway.common import _leader_profile_params
from hermes_team_mission.gateway.common import _leader_runtime_owner_error
from hermes_team_mission.gateway.common import _resolve_team_leader_runtime_params_for_request
from hermes_team_mission.gateway.common import _TEAM_LEADER_TOOLSET_SCOPE
from hermes_team_mission.gateway.common import bind_team_mission_session_workspace
from hermes_team_mission.gateway.common import get_hermes_home
from hermes_team_mission.gateway.common import resolve_team_mission_workspace_context
from hermes_team_mission.runtime.leader_runs import ensure_team_leader_message_run_state


RunSubmitter = Callable[[str, dict], dict]


def _home_from_dovie_profile(dovie_profile: dict) -> str:
    if isinstance(dovie_profile, dict):
        home = str(
            dovie_profile.get("hermesHomePath")
            or dovie_profile.get("hermes_home_path")
            or dovie_profile.get("hermes_home")
            or ""
        ).strip()
        if home:
            return home
    return str(resolve_default_agent_dir(Path(get_hermes_home())))


def _home_from_profile_params(profile_params: dict) -> str:
    profile_params = profile_params if isinstance(profile_params, dict) else {}
    dovie_profile = (
        profile_params.get("dovie_profile")
        if isinstance(profile_params.get("dovie_profile"), dict)
        else {}
    )
    home = str(
        profile_params.get("hermesHomePath")
        or profile_params.get("hermes_home_path")
        or profile_params.get("hermes_home")
        or ""
    ).strip()
    if home:
        return home
    return _home_from_dovie_profile(dovie_profile)


def _run_context_json(run_context: RunContext) -> str:
    return json.dumps(run_context.to_payload(), ensure_ascii=False)


def _control_plane_home() -> str:
    return str(os.getenv("DOVIE_HERMES_CONTROL_HOME") or get_hermes_home()).strip()


def _leader_report_prompt(
    *,
    mission: dict,
    result: dict,
    outcome: str,
    summary_text: str,
    artifact_refs: list[dict],
) -> str:
    node_results = [
        {
            "kind": str(item.get("kind") or ""),
            "title": str(item.get("title") or "")[:160],
            "status": str(item.get("status") or ""),
            "result": str(item.get("result") or "")[:500],
            "summary": str(item.get("summary") or "")[:700],
            "artifact_refs": [
                {
                    "title": str(ref.get("title") or ref.get("name") or ref.get("path") or "")[:200],
                    "path": str(ref.get("path") or ref.get("uri") or ref.get("url") or "")[:500],
                    "kind": str(ref.get("kind") or ref.get("type") or ""),
                }
                for ref in (item.get("artifact_refs") or item.get("artifactRefs") or [])
                if isinstance(ref, dict)
            ][:8],
        }
        for item in (result.get("node_results") or result.get("nodeResults") or [])
        if isinstance(item, dict)
    ][:16]
    report_context = {
        "mission": {
            "id": str(mission.get("mission_id") or mission.get("missionId") or ""),
            "title": str(mission.get("title") or "")[:200],
            "objective": str(mission.get("objective") or "")[:1000],
            "status": str(mission.get("status") or outcome or ""),
        },
        "result": {
            "id": str(result.get("result_id") or result.get("resultId") or ""),
            "outcome": str(result.get("outcome") or outcome or ""),
            "summary_text": str(result.get("summary_text") or result.get("summaryText") or summary_text or "")[:3000],
            "node_results": node_results,
            "artifact_refs": [
                {
                    "title": str(ref.get("title") or ref.get("name") or ref.get("path") or "")[:200],
                    "path": str(ref.get("path") or ref.get("uri") or ref.get("url") or "")[:500],
                    "kind": str(ref.get("kind") or ref.get("type") or ""),
                }
                for ref in artifact_refs
                if isinstance(ref, dict)
            ][:24],
        },
    }
    return "\n".join([
        "You are the Team Leader in a Dovie team conversation.",
        "The team task has reached a terminal state and you were asynchronously woken to report the result to the user.",
        "Write one natural user-facing Leader message in the user's language.",
        "Focus on the delivered result, conclusion, and useful next step. Do not present raw node/task execution status as the answer.",
        "Do not expose internal IDs, framework names, protocol names, tool calls, handoff details, or implementation mechanics.",
        "Do not say '当前进度如下' or produce a mechanical status dump.",
        "If files were produced, mention the key deliverables naturally. Artifact cards are attached separately, so do not paste long file paths unless they are essential.",
        "Do not call tools, do not start another team task, and do not ask the user to approve anything in this report turn.",
        "",
        "Mission result context:",
        json.dumps(report_context, ensure_ascii=False, indent=2),
    ]).strip()


def submit_mission_leader_report_run(
    *,
    db: Any,
    run_submitter: RunSubmitter,
    mission_id: str,
    synthesis_node_id: str = "",
    conversation_session_id: str = "",
    outcome: str,
    summary_text: str = "",
    mission: dict | None = None,
    result: dict | None = None,
    artifact_refs: list[dict] | None = None,
) -> dict:
    mission_id = str(mission_id or "").strip()
    if not mission_id:
        return {"ok": False, "status": "invalid", "error": "mission_id_required"}
    graph = db.get_team_mission_graph(mission_id) if callable(getattr(db, "get_team_mission_graph", None)) else {}
    graph = graph if isinstance(graph, dict) else {}
    mission = dict(mission or graph.get("mission") or {})
    result = dict(result or (db.get_team_mission_result(mission_id) if callable(getattr(db, "get_team_mission_result", None)) else {}) or {})
    existing_run_id = str(result.get("leader_report_run_id") or result.get("leaderReportRunId") or "").strip()
    if existing_run_id:
        return {
            "ok": True,
            "status": "already_requested",
            "mission_id": mission_id,
            "missionId": mission_id,
            "run_id": existing_run_id,
            "runId": existing_run_id,
            "conversation_session_id": str(conversation_session_id or ""),
            "conversationSessionId": str(conversation_session_id or ""),
        }
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    conversation_id = str(
        mission.get("conversation_id")
        or metadata.get("conversation_id")
        or metadata.get("conversationId")
        or mission_id
    ).strip()
    conversation_session_id = str(
        conversation_session_id
        or metadata.get("stableTeamSessionId")
        or metadata.get("stable_team_session_id")
        or metadata.get("conversation_session_id")
        or metadata.get("conversationSessionId")
        or mission.get("leader_session_id")
        or conversation_id
    ).strip()
    conversation = {}
    if conversation_id and callable(getattr(db, "get_team_mission_conversation", None)):
        try:
            conversation = db.get_team_mission_conversation(conversation_id) or {}
        except Exception:
            conversation = {}
    team_id = str(mission.get("team_id") or metadata.get("team_id") or metadata.get("teamId") or "")
    workspace_id = str(mission.get("workspace_id") or metadata.get("workspace_id") or metadata.get("workspaceId") or "")
    workspace_path = str(mission.get("workspace_path") or metadata.get("workspace_path") or metadata.get("workspacePath") or "")
    params = {
        "mission_id": mission_id,
        "missionId": mission_id,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        "team_id": team_id,
        "teamId": team_id,
        "workspace_id": workspace_id,
        "workspaceId": workspace_id,
        "workspace_path": workspace_path,
        "workspacePath": workspace_path,
    }
    try:
        params, leader_runtime_context = _resolve_team_leader_runtime_params_for_request(params, graph, db)
    except ValueError as exc:
        return {"ok": False, "status": "failed", "error": str(exc), "mission_id": mission_id, "missionId": mission_id}
    profile_params = _leader_profile_params(params, graph)
    runtime_scope_key = _leader_conversation_runtime_scope_key(
        params,
        conversation_id=conversation_id,
        mission_id=mission_id,
    )
    contract_error = _leader_conversation_runtime_scope_contract_error(params, runtime_scope_key)
    if contract_error:
        return {"ok": False, "status": "failed", "error": contract_error, "mission_id": mission_id, "missionId": mission_id}
    owner_error = _leader_runtime_owner_error(profile_params, leader_runtime_scope_key=runtime_scope_key)
    if owner_error:
        return {"ok": False, "status": "failed", "error": owner_error, "mission_id": mission_id, "missionId": mission_id}
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=mission,
            conversation=conversation if isinstance(conversation, dict) else {},
            session_id=conversation_session_id,
            require=True,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.leader_report",
                "conversation_id": conversation_id,
                "mission_id": mission_id,
                "team_id": str(params.get("team_id") or params.get("teamId") or ""),
            },
        )
        _ensure_team_conversation_session(db, conversation_session_id)
    except ValueError as exc:
        return {"ok": False, "status": "failed", "error": str(exc), "mission_id": mission_id, "missionId": mission_id}
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"team conversation session unavailable: {exc}",
            "mission_id": mission_id,
            "missionId": mission_id,
        }

    artifact_refs = [
        dict(item)
        for item in (artifact_refs or result.get("artifact_refs") or result.get("artifactRefs") or [])
        if isinstance(item, dict)
    ]
    run_id = uuid.uuid4().hex
    turn_id = uuid.uuid4().hex
    run_context = RunContext(
        conversation_session_id=conversation_session_id,
        participant_id=leader_participant_id(conversation_id),
        activity_id=f"chat:{conversation_session_id}",
        activity_kind="chat",
        execution_scope_key=runtime_scope_key,
        control_home=_control_plane_home(),
        execution_home=_home_from_profile_params(profile_params),
    )
    run_context_json = _run_context_json(run_context)
    result_id = str(result.get("result_id") or result.get("resultId") or "").strip()
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id="",
        run_id=run_id,
        session_id=conversation_session_id,
        runtime_session_id="",
        runtime_scope_key=runtime_scope_key,
        role="leader",
        metadata={
            "kind": "leader_report",
            "source": "team_mission.leader_report",
            "result_id": result_id,
            "resultId": result_id,
            "outcome": str(result.get("outcome") or outcome or ""),
            "source_node_id": str(synthesis_node_id or ""),
            "sourceNodeId": str(synthesis_node_id or ""),
            "artifact_refs": artifact_refs,
            "artifactRefs": artifact_refs,
            "run_context_json": run_context_json,
        },
    )
    prompt = _leader_report_prompt(
        mission=mission,
        result=result,
        outcome=outcome,
        summary_text=summary_text,
        artifact_refs=artifact_refs,
    )
    submit_params = {
        **params,
        **profile_params,
        "stored_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        "run_context_json": run_context_json,
        "agent_context_mode": "team_leader",
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        "text": prompt,
        "persist_user_message": "",
        "draft_text": "",
        "enabled_toolsets": [],
        "disabled_toolsets": _leader_disabled_toolsets(params),
        "toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE,
        "dovie_product_context": {
            **(params.get("dovie_product_context") if isinstance(params.get("dovie_product_context"), dict) else {}),
            "team_mission": {
                "kind": "leader_report",
                "surface": "mission_report",
                "mission_id": mission_id,
                "missionId": mission_id,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "conversation_session_id": conversation_session_id,
                "conversationSessionId": conversation_session_id,
                "workspace_id": workspace_context["workspace_id"],
                "workspace_path": workspace_context["workspace_path"],
                "result": result,
                "summary_text": str(result.get("summary_text") or result.get("summaryText") or summary_text or ""),
                "summaryText": str(result.get("summary_text") or result.get("summaryText") or summary_text or ""),
                "artifact_refs": artifact_refs,
                "artifactRefs": artifact_refs,
            },
        },
    }
    runtime_session_error = _ensure_team_mission_runtime_session_shell(conversation_session_id)
    if runtime_session_error:
        return {"ok": False, "status": "failed", "error": runtime_session_error, "mission_id": mission_id, "missionId": mission_id}
    response = run_submitter(f"leader-report:{run_id}", submit_params)
    if isinstance(response, dict) and response.get("error"):
        return {
            "ok": False,
            "status": "failed",
            "error": response.get("error"),
            "mission_id": mission_id,
            "missionId": mission_id,
            "run_id": run_id,
            "runId": run_id,
        }
    response_result = response.get("result") if isinstance(response, dict) else {}
    result_upsert = getattr(db, "upsert_team_mission_result", None)
    if callable(result_upsert):
        result_upsert(
            result_id=result_id,
            mission_id=mission_id,
            activity_id=str(result.get("activity_id") or result.get("activityId") or f"mission:{mission_id}"),
            status=str(result.get("status") or outcome or "completed"),
            outcome=str(result.get("outcome") or outcome or "completed"),
            summary_text=str(result.get("summary_text") or result.get("summaryText") or summary_text or ""),
            node_results=[
                dict(item)
                for item in (result.get("node_results") or result.get("nodeResults") or [])
                if isinstance(item, dict)
            ],
            artifact_refs=artifact_refs,
            leader_report_run_id=run_id,
            leader_report_message_id=str(result.get("leader_report_message_id") or result.get("leaderReportMessageId") or ""),
            metadata={
                **(result.get("metadata") if isinstance(result.get("metadata"), dict) else {}),
                "leader_report": {
                    "kind": "leader_report",
                    "run_id": run_id,
                    "status": "requested",
                    "source": "team_mission.leader_report",
                },
            },
        )
    ensure_team_leader_message_run_state(
        db,
        run_id=run_id,
        session_id=conversation_session_id,
        runtime_scope_key=runtime_scope_key,
        result=response_result if isinstance(response_result, dict) else {},
    )
    return {
        "ok": True,
        "status": "queued",
        "mission_id": mission_id,
        "missionId": mission_id,
        "result_id": result_id,
        "resultId": result_id,
        "run_id": run_id,
        "runId": run_id,
        "turn_id": turn_id,
        "turnId": turn_id,
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        "leader_runtime_context": leader_runtime_context,
        "leaderRuntimeContext": leader_runtime_context,
    }
