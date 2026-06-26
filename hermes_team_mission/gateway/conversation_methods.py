# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from .common import *


@method("team_capability.snapshot.get")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    snapshot_id = _team_capability_snapshot_id(params) or str(params.get("snapshot_id") or params.get("snapshotId") or "").strip()
    if snapshot_id:
        snapshot = db.get_team_capability_snapshot(snapshot_id)
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
    db = _get_db()
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
        graph = db.get_team_mission_graph(mission_id)
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
        snapshot = db.get_team_capability_snapshot(snapshot_id) if snapshot_id else {}
        binding = {}
        source = "snapshot_id" if snapshot else ""
        if mission_id:
            binding = db.get_team_capability_snapshot_binding(mission_id)
            if not snapshot:
                snapshot = db.get_bound_team_capability_snapshot(mission_id)
                if snapshot:
                    source = "mission_binding"
        if not snapshot:
            snapshot = db.get_latest_team_capability_snapshot(team_id) if team_id else {}
            if snapshot:
                source = "latest_team_snapshot"
        if not snapshot and team_id:
            snapshot = _resolve_team_capability_snapshot_from_registry(db, params, team_id=team_id)
            if snapshot:
                source = "team_registry"
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
                stable_session_id=conversation_session_id,
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
        return _ok(rid, {
            "mission_id": "",
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "conversation": conversation,
            "graph": {
                "mission": {},
                "conversation": conversation,
                "nodes": [],
                "edges": [],
                "run_bindings": [],
            },
        })
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
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
    if capability_snapshot:
        try:
            _bind_team_capability_snapshot_for_mission(
                db,
                mission_id=mission_id,
                conversation_id=conversation_id,
                snapshot=capability_snapshot,
            )
            graph = db.get_team_mission_graph(mission_id)
        except Exception as exc:
            return _err(rid, 5008, f"team capability snapshot bind failed: {exc}")
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
        graph = db.get_team_mission_graph(mission_id)
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
        start_response = _methods["team_mission.node.start"](
            rid,
            {
                "mission_id": mission_id,
                "node_id": str(root_node.get("node_id") or ""),
                "use_strategy_prompt": True,
                "record_user_task_message": params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"),
                "members": members,
            },
        )
        if isinstance(start_response, dict) and start_response.get("error"):
            return start_response
        graph = db.get_team_mission_graph(mission_id)
    result = {"mission_id": mission_id, "conversation_id": conversation_id, "graph": graph}
    if isinstance(start_response, dict):
        result["leader_start"] = start_response.get("result") or {}
    return _ok(rid, result)


@method("team_mission.conversation.ensure")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    graph = db.get_team_mission_graph(mission_id) if mission_id else {}
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    conversation_id = (
        _conversation_id_from_params(params, metadata)
        or (str(mission.get("conversation_id") or "").strip() if isinstance(mission, dict) else "")
        or mission_id
    )
    conversation_session_id = _conversation_session_id_from_params(params, metadata)
    if not conversation_session_id and isinstance(mission, dict) and mission:
        conversation_session_id = _team_conversation_session_id(mission)
    if not conversation_id and conversation_session_id:
        conversation_id = conversation_session_id
    if not conversation_id:
        return _err(rid, 4006, "conversation_id required")
    params = {
        **params,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        **({"conversation_session_id": conversation_session_id, "conversationSessionId": conversation_session_id} if conversation_session_id else {}),
        **({"mission_id": mission_id, "missionId": mission_id} if mission_id else {}),
    }
    before = db.get_team_mission_conversation(conversation_id)
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
            stable_session_id=conversation_session_id,
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
        (conversation or {}).get("stable_session_id")
        or conversation_session_id
        or ""
    )
    graph = db.get_team_mission_graph(mission_id) if mission_id else {}
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "created": created,
            "conversation": conversation,
            "session": db.get_session(conversation_session_id) or {},
            "leader_runtime_context": leader_runtime_context,
            "leaderRuntimeContext": leader_runtime_context,
            "graph": graph if isinstance(graph, dict) else {},
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
        return _ok(rid, {"conversation": {}, "mission": {}, "graph": {}})
    conversation = result.get("conversation") if isinstance(result, dict) else None
    _recover_conversation_active_run(db, conversation)
    if isinstance(conversation, dict):
        refreshed_identifier = str(
            conversation.get("conversation_id")
            or conversation.get("stable_session_id")
            or identifier
        ).strip()
        refreshed = db.resolve_team_mission_conversation(refreshed_identifier) if refreshed_identifier else {}
        if isinstance(refreshed, dict) and refreshed:
            result = refreshed
            conversation = result.get("conversation") if isinstance(result.get("conversation"), dict) else conversation
    if isinstance(conversation, dict):
        conversation.update(_conversation_runtime_projection(db, conversation))
    return _ok(rid, _attach_team_detail_projection(db, result))


@method("team_mission.conversation.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
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
    return _ok(rid, {"conversations": conversations})


@method("team_mission.conversation.runtime_session_ids")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _ok(rid, {"session_ids": [], "runtime_session_ids": []})
    getter = getattr(db, "list_team_mission_conversation_runtime_session_ids", None)
    if not callable(getter):
        return _ok(rid, {"session_ids": [], "runtime_session_ids": []})
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
        "runtime_session_ids": normalized,
        "runtimeSessionIds": normalized,
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
    return _ok(rid, result)


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
    stable_session_id = str(conversation.get("stable_session_id") or "").strip()
    if stable_session_id:
        run_state = run_control.session_status(
            stable_session_id,
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
        if stable_session_id:
            result.setdefault("stable_session_id", stable_session_id)
    except Exception as exc:
        return _err(rid, 5008, f"team mission conversation delete failed: {exc}")
    if not result:
        return _err(rid, 4040, "team mission conversation not found")
    return _ok(rid, result)


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
