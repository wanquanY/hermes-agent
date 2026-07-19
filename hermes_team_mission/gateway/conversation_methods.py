# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from .common import *
from .participant_autocreate import ensure_team_conversation_participants
from .public_conversation_identity import (
    public_team_conversation,
    public_team_conversation_list,
    public_team_conversation_result,
    public_team_mission_graph,
)
from hermes_team_mission.domain.activity import ACTIVITY_ID_FORMAT_PATTERN


def _get_existing_db():
    try:
        return _get_db(create_if_missing=False)
    except TypeError:
        return _get_db()


def _activity_id_from_params(params: dict) -> str:
    activity_id = str(params.get("activity_id") or params.get("activityId") or "").strip()
    if activity_id and not ACTIVITY_ID_FORMAT_PATTERN.match(activity_id):
        raise ValueError("activity_id malformed")
    return activity_id


def _list_field(source: dict, *keys: str) -> list:
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            return value
    return []


def _mission_metadata_team_profile_snapshot(
    mission: dict,
    *,
    mission_id: str = "",
    team_id: str = "",
) -> dict:
    mission = mission if isinstance(mission, dict) else {}
    metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
    members = metadata.get("members") if isinstance(metadata.get("members"), list) else []
    member_profiles = []
    for member in members:
        if not isinstance(member, dict):
            continue
        status = str(member.get("status") or "").strip().lower()
        if status in {"disabled", "removed"}:
            continue
        raw_dovie_profile = member.get("dovie_profile")
        dovie_profile = raw_dovie_profile if isinstance(raw_dovie_profile, dict) else {}
        member_id = str(
            member.get("member_id") or member.get("memberId") or member.get("id") or ""
        ).strip()
        if not member_id:
            continue
        member_profiles.append({
            "member_id": member_id,
            "agent_profile_id": str(
                member.get("profile_id")
                or member.get("profileId")
                or member.get("agent_profile_id")
                or member.get("agentProfileId")
                or ""
            ).strip(),
            "agent_profile_version_id": str(
                member.get("profile_version_id")
                or member.get("profileVersionId")
                or member.get("agent_profile_version_id")
                or member.get("agentProfileVersionId")
                or ""
            ).strip(),
            "display_name": str(
                member.get("display_name")
                or member.get("displayName")
                or dovie_profile.get("name")
                or member_id
            ).strip(),
            "role": str(member.get("role") or "member").strip(),
            "profile_description": str(
                member.get("profile_summary")
                or member.get("profileSummary")
                or dovie_profile.get("description")
                or member.get("description")
                or ""
            ).strip(),
            "capability_tags": _list_field(member, "capability_tags", "capabilityTags"),
            "best_for_tasks": _list_field(member, "best_for_tasks", "bestForTasks"),
            "avoid_tasks": _list_field(member, "avoid_tasks", "avoidTasks"),
            "strengths": _list_field(member, "strengths"),
            "limitations": _list_field(member, "limitations"),
            "default_toolsets": _list_field(member, "default_toolsets", "defaultToolsets"),
            "recommended_skills": _list_field(member, "recommended_skills", "recommendedSkills"),
            "radar_scores": _list_field(member, "radar_scores", "radarScores"),
        })
    if not member_profiles:
        return {}
    resolved_mission_id = str(
        mission_id or mission.get("mission_id") or mission.get("missionId") or ""
    ).strip()
    resolved_team_id = str(
        team_id or mission.get("team_id") or mission.get("teamId") or ""
    ).strip()
    snapshot_id = (
        f"mission-metadata:{resolved_mission_id}"
        if resolved_mission_id
        else "mission-metadata"
    )
    return {
        "snapshot_id": snapshot_id,
        "team_id": resolved_team_id,
        "version": 0,
        "status": "ready",
        "team_profile": {
            "display_name": str(mission.get("title") or metadata.get("title") or "").strip(),
            "collaboration_mode": str(mission.get("mode") or metadata.get("mode_strategy") or "").strip(),
            "positioning": str(mission.get("objective") or metadata.get("objective") or "").strip(),
        },
        "member_profiles": member_profiles,
    }


def _mission_metadata_fallback_from_params(params: dict, *, mission_id: str, team_id: str) -> dict:
    metadata = params.get("mission_metadata") if isinstance(params.get("mission_metadata"), dict) else {}
    if not metadata:
        return {}
    return {
        "mission_id": mission_id,
        "team_id": team_id,
        "title": str(params.get("mission_title") or params.get("missionTitle") or "").strip(),
        "objective": str(
            params.get("mission_objective") or params.get("missionObjective") or ""
        ).strip(),
        "mode": str(params.get("mission_mode") or params.get("missionMode") or "").strip(),
        "metadata": metadata,
    }


@method("team_capability.snapshot.get")
def _(rid, params: dict) -> dict:
    db = _get_existing_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    snapshot_id = _team_capability_snapshot_id(params) or str(params.get("snapshot_id") or params.get("snapshotId") or "").strip()
    if snapshot_id:
        snapshot = db.team_capabilities.get(snapshot_id)
        if not snapshot:
            return _err(rid, 4040, "team capability snapshot not found")
        return _ok(rid, {"snapshot": snapshot})
    team_id = str(params.get("team_id") or params.get("teamId") or "").strip()
    if not team_id:
        return _err(rid, 4006, "team_id or snapshot_id required")
    try:
        snapshot = _resolve_team_capability_snapshot_from_registry(db, params, team_id=team_id)
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot unavailable: {exc}")
    return _ok(rid, {"snapshot": snapshot})


@method("team_capability.snapshot.refresh")
def _(rid, params: dict) -> dict:
    db = _get_existing_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    team_id = str(params.get("team_id") or params.get("teamId") or "").strip()
    if not team_id:
        return _err(rid, 4006, "team_id required")
    try:
        snapshot = _resolve_team_capability_snapshot_from_registry(db, params, team_id=team_id, force_refresh=True)
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot refresh failed: {exc}")
    return _ok(rid, {"snapshot": snapshot})


@method("team_capability.snapshot.bind")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        snapshot = _resolve_team_capability_snapshot_for_params(
            db,
            params,
            team_id=str(params.get("team_id") or params.get("teamId") or ""),
        )
        if not snapshot:
            return _err(rid, 4006, "snapshot_id or team_id required")
        binding = _bind_team_capability_snapshot_for_mission(
            db,
            mission_id=mission_id,
            conversation_id=_conversation_id_from_params(params, {}),
            snapshot=snapshot,
        )
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot bind failed: {exc}")
    return _ok(rid, {"snapshot": snapshot, "binding": binding})


@method("team_mission.team_profile.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    snapshot_id = _team_capability_snapshot_id(params) or str(params.get("snapshot_id") or params.get("snapshotId") or "").strip()
    mission = {}
    conversation = {}
    if mission_id:
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) and isinstance(graph.get("mission"), dict) else {}
    if not mission_id:
        identifier = (
            _conversation_id_from_params(params, {})
            or _conversation_session_id_from_params(params, {})
            or str(params.get("identifier") or params.get("id") or "").strip()
        )
        resolved = db.resolve_team_mission_conversation(identifier) if identifier else {}
        conversation = resolved.get("conversation") if isinstance(resolved, dict) and isinstance(resolved.get("conversation"), dict) else {}
        mission = resolved.get("mission") if isinstance(resolved, dict) and isinstance(resolved.get("mission"), dict) else {}
        mission_id = str(mission.get("mission_id") or "").strip()
    try:
        team_id = _team_id_for_profile(params, mission=mission, conversation=conversation)
        snapshot = db.team_capabilities.get(snapshot_id) if snapshot_id else {}
        binding = {}
        source = "snapshot_id" if snapshot else ""
        if mission_id:
            binding = db.team_capabilities.get_binding(mission_id)
            if not snapshot:
                snapshot = db.team_capabilities.get_bound(mission_id)
                if snapshot:
                    source = "mission_binding"
        if not snapshot:
            snapshot = db.team_capabilities.get_latest(team_id) if team_id else {}
            if snapshot:
                source = "latest_team_snapshot"
        registry_error = ""
        if not snapshot and team_id:
            try:
                snapshot = _resolve_team_capability_snapshot_from_registry(db, params, team_id=team_id)
                if snapshot:
                    source = "team_registry"
            except Exception as exc:
                registry_error = str(exc)
        if not snapshot:
            metadata_mission = mission or _mission_metadata_fallback_from_params(
                params,
                mission_id=mission_id,
                team_id=team_id,
            )
            snapshot = _mission_metadata_team_profile_snapshot(
                metadata_mission,
                mission_id=mission_id,
                team_id=team_id,
            )
            if snapshot:
                source = "mission_metadata_members"
        if not snapshot and registry_error:
            return _err(rid, 5008, f"team profile unavailable: {registry_error}")
    except Exception as exc:
        return _err(rid, 5008, f"team profile unavailable: {exc}")
    if not snapshot:
        return _err(rid, 4040, "team capability snapshot not found")
    return _ok(rid, {"mission_id": mission_id, "team_id": team_id, "binding": binding, "snapshot": snapshot, "source": source})


@method("team_mission.create")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    team_id = str(params.get("team_id") or params.get("teamId") or "").strip()
    archived_team_error = _archived_team_write_error(db, team_id)
    if archived_team_error:
        return _err(rid, 4023, archived_team_error)
    try:
        mode = _resolve_team_mission_create_mode(db, params, team_id=team_id)
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    members = []
    graph_payload = (
        params.get("graph_payload")
        or params.get("graphPayload")
        or {}
    )
    if not isinstance(graph_payload, dict):
        return _err(rid, 4004, "graph_payload must be an object")
    metadata = params.get("metadata") or {}
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "metadata must be an object")
    metadata = _normalize_mission_metadata(params, metadata)
    incoming_dovie_context = _dovie_product_context_from_params(params)
    if not incoming_dovie_context:
        incoming_dovie_context = _dovie_product_context_from_params({
            "dovie_product_context": (
                metadata.get("dovie_product_context")
                or metadata.get("dovieProductContext")
            ),
        })
    if incoming_dovie_context:
        metadata = {**metadata, "dovie_product_context": incoming_dovie_context}
    conversation_id = _conversation_id_from_params(params, metadata) or mission_id
    leader_session_id = str(params.get("leader_session_id") or params.get("leaderSessionId") or "").strip()
    if not leader_session_id:
        leader_session_id = _conversation_session_id_from_params(params, metadata)
    if _conversation_only_from_params(params):
        metadata = {
            **metadata,
            "conversation_only": True,
            "start_leader": False,
        }
        if not conversation_id:
            return _err(rid, 4006, "conversation_id required")
        conversation_session_id = _conversation_session_id_from_params(params, metadata) or conversation_id
        try:
            workspace_context = resolve_team_mission_workspace_context(
                params,
                session_id=conversation_session_id,
                require=True,
            )
        except ValueError as exc:
            return _err(rid, 4004, str(exc))
        try:
            conversation = db.ensure_team_mission_conversation(
                conversation_id=conversation_id,
                conversation_session_id=conversation_session_id,
                team_id=team_id,
                title=str(params.get("title") or ""),
                objective=str(params.get("conversation_objective") or params.get("conversationObjective") or ""),
                workspace_id=workspace_context["workspace_id"],
                workspace_path=workspace_context["workspace_path"],
                created_by_user_id=str(params.get("created_by_user_id") or params.get("createdByUserId") or ""),
                metadata=metadata,
            )
            bind_team_mission_session_workspace(
                session_id=conversation_session_id,
                context=workspace_context,
                metadata={
                    "source": "team_mission.create",
                    "conversation_id": conversation_id,
                    "team_id": team_id,
                    "conversation_only": True,
                },
            )
        except ValueError as exc:
            return _err(rid, 4004, str(exc))
        except Exception as exc:
            return _err(rid, 5008, f"team mission conversation create failed: {exc}")
        ensure_team_conversation_participants(
            db,
            conversation_session_id=conversation_session_id,
            team_id=team_id,
            members=None,
            source="team_mission.create",
        )
        return _ok(rid, {
            "mission_id": "",
            "conversation_session_id": conversation_session_id,
            "conversation": public_team_conversation(conversation),
            "graph": {
                "mission": {},
                "conversation": public_team_conversation(conversation),
                "nodes": [],
                "edges": [],
                "run_bindings": [],
            },
        })
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        request_activity_id = _activity_id_from_params(params)
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    if team_id:
        try:
            members = _team_runtime_members_from_registry(db, params, team_id=team_id)
        except ValueError as exc:
            return _err(rid, 4006, str(exc))
        except Exception as exc:
            return _err(rid, 5008, f"team members unavailable: {exc}")
    else:
        return _err(rid, 4006, "team_id required")
    capability_snapshot = {}
    try:
        capability_snapshot = _resolve_team_capability_snapshot_for_params(
            db,
            params,
            team_id=team_id,
        )
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team capability snapshot unavailable: {exc}")
    if capability_snapshot:
        metadata["team_capability_snapshot"] = _snapshot_binding_metadata(capability_snapshot)
        members = _members_with_capability_snapshot(members, capability_snapshot)
    conversation_session_id = leader_session_id or _conversation_session_id_from_params(params, metadata) or conversation_id
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            session_id=conversation_session_id,
            require=True,
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    try:
        graph = db.initialize_team_mission_from_strategy(
            mission_id=mission_id,
            conversation_id=conversation_id,
            team_id=team_id,
            title=str(params.get("title") or ""),
            objective=str(params.get("objective") or params.get("prompt") or ""),
            workspace_id=workspace_context["workspace_id"],
            workspace_path=workspace_context["workspace_path"],
            mode=mode,
            members=members,
            graph_payload=graph_payload,
            leader_session_id=leader_session_id,
            metadata=metadata,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.create",
                "conversation_id": conversation_id,
                "mission_id": mission_id,
                "team_id": team_id,
            },
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team mission create failed: {exc}")
    ensure_team_conversation_participants(
        db,
        conversation_session_id=conversation_session_id,
        team_id=team_id,
        members=members,
        leader_profile_params=_leader_profile_params(params, graph if isinstance(graph, dict) else {}),
        source="team_mission.create",
    )
    if capability_snapshot:
        try:
            _bind_team_capability_snapshot_for_mission(
                db,
                mission_id=mission_id,
                conversation_id=conversation_id,
                snapshot=capability_snapshot,
            )
            graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
        except Exception as exc:
            return _err(rid, 5008, f"team capability snapshot bind failed: {exc}")
    try:
        if request_activity_id:
            activity = db.activities.bind_to_mission(
                activity_id=request_activity_id,
                conversation_id=conversation_session_id,
                mission_id=mission_id,
                target_team_id=team_id,
                status="running",
                prompt_summary=str(params.get("title") or params.get("objective") or params.get("prompt") or ""),
            )
        else:
            activity = db.activities.ensure_mission(
                conversation_id=conversation_session_id,
                mission_id=mission_id,
                status="running",
                prompt_summary=str(params.get("title") or params.get("objective") or params.get("prompt") or ""),
            )
    except Exception as exc:
        return _err(rid, 5008, f"team mission activity create failed: {exc}")
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    root_node = next(
        (
            node for node in graph.get("nodes", [])
            if isinstance(node, dict) and str(node.get("kind") or "") == "root"
        ),
        None,
    )
    task_id = str(params.get("task_id") or params.get("taskId") or "").strip()
    if root_node and task_id:
        root_metadata = root_node.get("metadata") if isinstance(root_node.get("metadata"), dict) else {}
        root_metadata = {
            **root_metadata,
            "submitted_task_id": task_id,
            "task_id": task_id,
            "task_title": str(params.get("title") or root_node.get("title") or ""),
            "task_objective": str(params.get("objective") or params.get("prompt") or root_node.get("objective") or ""),
        }
        root_node = db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id=str(root_node.get("node_id") or ""),
            kind=str(root_node.get("kind") or "root"),
            title=str(root_node.get("title") or ""),
            objective=str(root_node.get("objective") or ""),
            status=str(root_node.get("status") or "running"),
            assignee_profile_id=str(root_node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(root_node.get("assignee_profile_version_id") or ""),
            runtime_scope_key=str(root_node.get("runtime_scope_key") or ""),
            output_contract=root_node.get("output_contract") if isinstance(root_node.get("output_contract"), dict) else {},
            metadata=root_metadata,
            position_x=float(root_node.get("position_x") or 0),
            position_y=float(root_node.get("position_y") or 0),
        )
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else {}
    if (
        isinstance(mission, dict)
        and mission
        and not _falsey(params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"))
    ):
        _append_team_user_task_message(
            db,
            mission=mission,
            objective=str(params.get("objective") or params.get("prompt") or ""),
            node_id=str((root_node or {}).get("node_id") or ""),
            task_id=task_id or mission_id,
        )
    start_response = None
    if root_node and metadata.get("start_leader") is True:
        start_params = {
            "mission_id": mission_id,
            "node_id": str(root_node.get("node_id") or ""),
            "use_strategy_prompt": True,
            "record_user_task_message": params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"),
            "members": members,
            "dispatch_activity_id": metadata.get("dispatch_activity_id") or request_activity_id,
            "parent_activity_id": metadata.get("parent_activity_id") or request_activity_id,
            "parent_conversation_id": metadata.get("parent_conversation_id"),
            "parent_scope_key": metadata.get("parent_scope_key"),
            "parent_hermes_home": metadata.get("parent_hermes_home"),
            "source": metadata.get("source") or "team_dispatch",
        }
        if incoming_dovie_context:
            start_params["dovie_product_context"] = incoming_dovie_context
        start_response = _methods["team_mission.node.start"](rid, start_params)
        if isinstance(start_response, dict) and start_response.get("error"):
            return start_response
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    activity_id = str((activity or {}).get("activity_id") or request_activity_id or f"mission:{mission_id}").strip()
    result = {
        "mission_id": mission_id,
        "activity_id": activity_id,
        "conversation_session_id": conversation_session_id,
        "graph": public_team_mission_graph(graph),
    }
    if isinstance(start_response, dict):
        result["leader_start"] = start_response.get("result") or {}
    return _ok(rid, result)


@method("team_mission.conversation.ensure")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id) if mission_id else {}
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    conversation_session_id = _conversation_session_id_from_params(params, metadata)
    if not conversation_session_id and isinstance(mission, dict) and mission:
        conversation_session_id = _team_conversation_session_id(mission)
    if not conversation_session_id:
        conversation_session_id = f"team-conversation-{uuid.uuid4().hex}"
    existing_conversation = (
        db.get_team_mission_conversation_by_session(conversation_session_id)
        if conversation_session_id
        else {}
    )
    conversation_id = (
        str((existing_conversation or {}).get("conversation_id") or "").strip()
        or (str(mission.get("conversation_id") or "").strip() if isinstance(mission, dict) else "")
        # Compatibility for callers that have not yet migrated.  New callers
        # never send this storage key.
        or _conversation_id_from_params(params, metadata)
        or conversation_session_id
        or mission_id
    )
    params = {
        **params,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        **({"conversation_session_id": conversation_session_id, "conversationSessionId": conversation_session_id} if conversation_session_id else {}),
        **({"mission_id": mission_id, "missionId": mission_id} if mission_id else {}),
    }
    before = existing_conversation or db.get_team_mission_conversation(conversation_id)
    archived_team_error = _archived_team_write_error(
        db,
        _team_id_for_profile(params, mission=mission if isinstance(mission, dict) else {}, conversation=before),
    )
    if archived_team_error:
        return _err(rid, 4023, archived_team_error)
    try:
        params, leader_runtime_context = _resolve_team_leader_runtime_params_for_request(
            params,
            graph if isinstance(graph, dict) else {},
            db,
        )
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    profile_params = _leader_profile_params(params, graph if isinstance(graph, dict) else {})
    runtime_scope_key = _leader_conversation_runtime_scope_key(
        params,
        conversation_id=conversation_id,
        mission_id=mission_id,
    )
    contract_error = _leader_conversation_runtime_scope_contract_error(params, runtime_scope_key)
    if contract_error:
        return _err(rid, 4094, contract_error)
    owner_error = _leader_runtime_owner_error(profile_params, leader_runtime_scope_key=runtime_scope_key)
    if owner_error:
        return _err(rid, 4094, owner_error)
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=mission if isinstance(mission, dict) else {},
            conversation=before if isinstance(before, dict) else {},
            session_id=conversation_session_id,
            require=True,
        )
        bound_mission_id = mission_id if isinstance(mission, dict) and mission else ""
        conversation = db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            mission=mission if isinstance(mission, dict) else {},
            mission_id=bound_mission_id,
            team_id=str(params.get("team_id") or params.get("teamId") or ""),
            title="",
            objective=str(params.get("objective") or params.get("prompt") or ""),
            workspace_id=workspace_context["workspace_id"],
            workspace_path=workspace_context["workspace_path"],
            created_by_user_id=str(params.get("created_by_user_id") or params.get("createdByUserId") or ""),
            metadata=metadata,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.conversation.ensure",
                "conversation_id": conversation_id,
                **({"mission_id": mission_id} if mission_id else {}),
                "team_id": str(params.get("team_id") or params.get("teamId") or ""),
            },
        )
        created = not bool(before)
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team mission conversation unavailable: {exc}")
    conversation_session_id = str(
        (conversation or {}).get("conversation_session_id")
        or conversation_session_id
        or ""
    )
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id) if mission_id else {}
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "conversation_session_id": conversation_session_id,
            "created": created,
            "conversation": public_team_conversation(conversation),
            "session": db.sessions.get(conversation_session_id) or {},
            "leader_runtime_context": leader_runtime_context,
            "leaderRuntimeContext": leader_runtime_context,
            "graph": public_team_mission_graph(graph),
        },
    )


@method("team_mission.conversation.resolve")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    identifier = str(
        params.get("identifier")
        or params.get("id")
        or _conversation_id_from_params(params, metadata)
        or _mission_id_from_params(params)
        or _conversation_session_id_from_params(params, metadata)
        or ""
    ).strip()
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    result = db.resolve_team_mission_conversation(identifier)
    if not result:
        return _err(rid, 4040, "team mission conversation not found")
    conversation = result.get("conversation") if isinstance(result, dict) else None
    _recover_conversation_active_run(db, conversation)
    if isinstance(conversation, dict):
        refreshed_identifier = str(
            conversation.get("conversation_id")
            or conversation.get("conversation_session_id")
            or identifier
        ).strip()
        refreshed = db.resolve_team_mission_conversation(refreshed_identifier) if refreshed_identifier else {}
        if isinstance(refreshed, dict) and refreshed:
            result = refreshed
            conversation = result.get("conversation") if isinstance(result.get("conversation"), dict) else conversation
    if isinstance(conversation, dict):
        conversation.update(_conversation_runtime_projection(db, conversation))
    return _ok(
        rid,
        public_team_conversation_result(_attach_team_detail_projection(db, result)),
    )


@method("team_mission.conversation.list")
def _(rid, params: dict) -> dict:
    """Superseded by enriched session.list per P4; kept for ABI compatibility."""

    db = _get_existing_db()
    if db is None:
        return _ok(rid, {"conversations": []})
    conversations = db.list_team_mission_conversations(
        team_id=str(params.get("team_id") or params.get("teamId") or ""),
        workspace_id=_workspace_id_from_params(params),
        status=str(params.get("status") or ""),
        limit=_bounded_limit(params.get("limit"), default=100, maximum=500),
    )
    # 列表级查询(侧栏)只需要列表字段;运行态由 session_index 预计算提供。lightweight
    # 模式跳过对每个会话的画布级富化(recover active run + runtime projection:每会话
    # 查 missions/nodes/run_bindings/消息 + 逐个查 run)。那是 O(N) 富化,会话越多越慢,
    # 而画布详情本就应在打开会话时才加载,不属于列表职责。其他客户端(不传 lightweight)
    # 保持完整富化,行为不变。
    lightweight = bool(params.get("lightweight") or params.get("lite"))
    if not lightweight:
        for conversation in conversations if isinstance(conversations, list) else []:
            _recover_conversation_active_run(db, conversation)
            conversation.update(_conversation_runtime_projection(db, conversation))
    return _ok(rid, {"conversations": public_team_conversation_list(conversations)})


@method("team_mission.conversation.participants")
def _(rid, params: dict) -> dict:
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    conversation_session_id = _conversation_session_id_from_params(params, metadata)
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    db = _get_db()
    if db is None:
        return _ok(
            rid,
            {
                "conversation_session_id": conversation_session_id,
                "conversationSessionId": conversation_session_id,
                "participants": [],
            },
        )
    participants = db.participants.list_conversation_participants(conversation_session_id)
    return _ok(
        rid,
        {
            "conversation_session_id": conversation_session_id,
            "conversationSessionId": conversation_session_id,
            "participants": participants if isinstance(participants, list) else [],
        },
    )


@method("team_mission.conversation.execution_session_ids")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"session_ids": [], "sessionIds": [], "execution_session_ids": [], "executionSessionIds": []})
    getter = getattr(db, "list_team_mission_conversation_execution_session_ids", None)
    if not callable(getter):
        return _ok(rid, {"session_ids": [], "sessionIds": [], "execution_session_ids": [], "executionSessionIds": []})
    session_ids = getter(
        team_id=str(params.get("team_id") or params.get("teamId") or ""),
        workspace_id=_workspace_id_from_params(params),
        status=str(params.get("status") or ""),
        mission_id=str(params.get("mission_id") or params.get("missionId") or ""),
        limit=_bounded_limit(params.get("limit"), default=500, maximum=500),
    )
    normalized = []
    seen = set()
    for session_id in session_ids if isinstance(session_ids, list) else []:
        value = str(session_id or "").strip()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return _ok(rid, {
        "session_ids": normalized,
        "sessionIds": normalized,
        "execution_session_ids": normalized,
        "executionSessionIds": normalized,
    })


@method("team_mission.conversation.rename")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    identifier = str(
        params.get("identifier")
        or params.get("id")
        or _conversation_id_from_params(params, metadata)
        or _mission_id_from_params(params)
        or _conversation_session_id_from_params(params, metadata)
        or ""
    ).strip()
    title = str(params.get("title") or "").strip()
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    if not title:
        return _err(rid, 4021, "title required")
    try:
        result = db.rename_team_mission_conversation(identifier, title)
    except ValueError as exc:
        return _err(rid, 4021, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team mission conversation rename failed: {exc}")
    if not result:
        return _err(rid, 4040, "team mission conversation not found")
    return _ok(rid, public_team_conversation_result(result))


@method("team_mission.conversation.delete")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    identifier = str(
        params.get("identifier")
        or params.get("id")
        or _conversation_id_from_params(params, metadata)
        or _mission_id_from_params(params)
        or _conversation_session_id_from_params(params, metadata)
        or ""
    ).strip()
    if not identifier:
        return _err(rid, 4006, "conversation identifier required")
    resolved = db.resolve_team_mission_conversation(identifier)
    conversation = resolved.get("conversation") if isinstance(resolved, dict) else {}
    if not conversation:
        return _err(rid, 4040, "team mission conversation not found")
    conversation_session_id = str(conversation.get("conversation_session_id") or "").strip()
    if conversation_session_id:
        run_state = run_control.session_status(
            conversation_session_id,
            db=db,
            current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
        )
        if run_state.get("running"):
            return _err(rid, 4023, "cannot delete a conversation with an active leader run")
    try:
        result = db.delete_team_mission_conversation(identifier)
        deleted_session_ids = list((result or {}).get("deleted_session_ids") or [])
        artifact_cleanup = {
            "deleted_artifact_links": 0,
            "deleted_artifacts": 0,
            "deleted_artifact_ids": [],
            "physical_files_deleted": 0,
        }
        workspace_bindings = []
        workspace_binding_cleanup_error = ""
        if deleted_session_ids:
            try:
                artifact_cleanup = delete_session_artifacts(deleted_session_ids)
            except Exception as exc:
                artifact_cleanup = {
                    **artifact_cleanup,
                    "error": str(exc),
                }
            try:
                workspace_bindings = delete_session_workspace_bindings(deleted_session_ids)
            except Exception as exc:
                workspace_bindings = []
                workspace_binding_cleanup_error = str(exc)
        remove_session_files = getattr(db, "_remove_session_files", None)
        if callable(remove_session_files):
            sessions_dir = Path(get_hermes_home()) / "sessions"
            for session_id in dict.fromkeys(str(item or "").strip() for item in deleted_session_ids):
                if not session_id:
                    continue
                try:
                    remove_session_files(sessions_dir, session_id)
                except Exception:
                    pass
        if isinstance(result, dict):
            result["deleted_session_ids"] = deleted_session_ids
            result["artifact_cleanup"] = artifact_cleanup
            result["deleted_artifact_links"] = int(artifact_cleanup.get("deleted_artifact_links") or 0)
            result["deleted_artifacts"] = int(artifact_cleanup.get("deleted_artifacts") or 0)
            result["physical_files_deleted"] = int(artifact_cleanup.get("physical_files_deleted") or 0)
            result["deleted_workspace_bindings"] = workspace_bindings
            result["deleted_workspace_binding_count"] = len(workspace_bindings)
            if workspace_binding_cleanup_error:
                result["workspace_binding_cleanup_error"] = workspace_binding_cleanup_error
        if conversation_session_id:
            result.setdefault("conversation_session_id", conversation_session_id)
    except Exception as exc:
        return _err(rid, 5008, f"team mission conversation delete failed: {exc}")
    if not result:
        return _err(rid, 4040, "team mission conversation not found")
    return _ok(rid, public_team_conversation_result(result))


# ── Group-chat: direct-to-member (decoupled from team missions) ──────
# A user can @-mention a worker member in the team conversation to talk to it
# directly (bypassing the leader). This is its OWN mechanism, independent of the
# team-mission/task machinery: no mission, no graph node, no run-binding, no
# terminal-mission reaper. The member runs as a turn that (a) is fed the full
# shared conversation transcript as context, and (b) relays its reply back into
# the conversation session tagged with the member's identity — so everything
# (leader chats, member @-chats, task outputs) shares one conversation log that
# the leader and members all read. Implementation in progress.


# Export underscore-prefixed helpers for the compatibility facade.
__all__ = [name for name in globals() if not name.startswith("__")]
