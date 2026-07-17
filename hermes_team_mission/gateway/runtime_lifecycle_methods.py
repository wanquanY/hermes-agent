# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from .runtime_methods import *


@method("team_mission.plan.reject")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        _log.info(
            "[team_mission.plan.reject] rejected: db unavailable rid=%s",
            rid,
        )
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        _log.info(
            "[team_mission.plan.reject] rejected: mission_id missing rid=%s",
            rid,
        )
        return _err(rid, 4006, "mission_id required")
    task_id = str(params.get("task_id") or params.get("taskId") or "")
    rejected_by = str(params.get("rejected_by") or params.get("rejectedBy") or "user")
    run_id = _run_id_from_params(params)
    _log.info(
        "[team_mission.plan.reject] enter mission_id=%s task_id=%s rejected_by=%s run_id=%s rid=%s",
        mission_id,
        task_id,
        rejected_by,
        run_id,
        rid,
    )
    result = db.reject_team_mission_plan(
        mission_id=mission_id,
        task_id=task_id,
        rejected_by=rejected_by,
        reason=str(params.get("reason") or ""),
        run_id=run_id,
    )
    if not result:
        _log.warning(
            "[team_mission.plan.reject] mission not found mission_id=%s task_id=%s rid=%s",
            mission_id,
            task_id,
            rid,
        )
        return _err(rid, 4040, "team mission not found")
    canceled_nodes = list(result.get("canceled_nodes") or [])
    # Live worker termination — mirror team_mission.cancel handler line 1998+.
    # session_graph.reject_team_mission_plan reap 了 run 表状态,这里还要显式
    # 调 run.cancel 让 worker 进程真的停下来。不 cancel worker,后续 plan_complete
    # 等事件会继续 emit,前端重算 mission 状态 → 审批卡二次弹窗。
    reason = str(params.get("reason") or "")
    canceled_runs: list[dict] = []
    cancel_errors: list[dict] = []
    seen_run_ids: set[str] = set()
    for binding in result.get("cancel_run_bindings") or []:
        if not isinstance(binding, dict):
            continue
        bound_run_id = str(binding.get("run_id") or "").strip()
        if not bound_run_id or bound_run_id in seen_run_ids:
            continue
        seen_run_ids.add(bound_run_id)
        conversation_session_id = str(
            binding.get("session_id") or binding.get("conversation_session_id") or ""
        ).strip()
        cancel_params = {
            "run_id": bound_run_id,
            "conversation_session_id": conversation_session_id,
            "execution_session_id": str(binding.get("execution_session_id") or ""),
            "runtime_scope_key": str(
                binding.get("runtime_scope_key") or conversation_session_id
            ),
            "reason": reason or "用户拒绝了团队任务图计划。",
        }
        try:
            response = _methods["run.cancel"](rid, cancel_params)
        except Exception as exc:
            cancel_errors.append({"run_id": bound_run_id, "message": str(exc)})
            continue
        if isinstance(response, dict) and response.get("error"):
            error = (
                response.get("error") if isinstance(response.get("error"), dict) else {}
            )
            cancel_errors.append({
                "run_id": bound_run_id,
                "message": str(
                    error.get("message") or response.get("error") or "run cancel failed"
                ),
            })
            continue
        response_result = (
            response.get("result")
            if isinstance(response, dict) and isinstance(response.get("result"), dict)
            else {}
        )
        canceled_runs.append({
            "run_id": bound_run_id,
            "conversation_session_id": conversation_session_id,
            "status": str(response_result.get("status") or "cancelled"),
            "turn_id": str(response_result.get("turn_id") or ""),
        })
    _log.info(
        "[team_mission.plan.reject] ok mission_id=%s task_id=%s canceled_nodes=%d canceled_runs=%d cancel_errors=%d rid=%s",
        mission_id,
        result.get("task_id") or task_id,
        len(canceled_nodes),
        len(canceled_runs),
        len(cancel_errors),
        rid,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "task_id": result.get("task_id") or "",
            "canceled_nodes": canceled_nodes,
            "canceled_runs": canceled_runs,
            "cancel_errors": cancel_errors,
            "graph": result.get("graph") or {},
        },
    )


def _recall_collect_conv_message_ids(
    db, *, conversation_session_id: str, turn_id: str
) -> list[int]:
    """Find the conv messages that ``session.recall_turn`` is about to
    deactivate for ``turn_id`` — the user message stamped with the turn id,
    plus every subsequent active assistant / mirror row up to (but not
    including) the next user message. We snapshot these BEFORE the recall so
    we can mirror the deactivation into every member-chat view session that
    has materialized rows pointing back at them.
    """
    try:
        msgs = db.messages.list(conversation_session_id) or []
    except Exception:
        return []
    turn_id = str(turn_id or "").strip()
    if not turn_id:
        return []
    found_target = False
    collected: list[int] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if not found_target:
            meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
            if isinstance(m.get("metadata"), str):
                try:
                    meta = json.loads(m.get("metadata") or "{}")
                    if not isinstance(meta, dict):
                        meta = {}
                except Exception:
                    meta = {}
            mt_meta = (
                meta.get("team_mission")
                if isinstance(meta.get("team_mission"), dict)
                else {}
            )
            if str(m.get("role") or "").lower() == "user" and (
                str(meta.get("turn_id") or "") == turn_id
                or str(mt_meta.get("turn_id") or "") == turn_id
            ):
                found_target = True
                mid = m.get("id")
                if mid is not None:
                    try:
                        collected.append(int(mid))
                    except (TypeError, ValueError):
                        pass
            continue
        # After the target user message — collect until the next user message.
        if str(m.get("role") or "").lower() == "user":
            break
        mid = m.get("id")
        if mid is not None:
            try:
                collected.append(int(mid))
            except (TypeError, ValueError):
                pass
    return collected


def _recall_mission_id_from_run(
    db, *, run_id: str, params: dict, mission: dict | None
) -> str:
    """Resolve the mission id (if any) this recall should cascade-cancel.

    Order: explicit param > canonical team_mission_run_bindings > persisted
    run state > resolved active mission context. Returns ``""`` for non-mission
    turns, in which case the caller skips ``team_mission.cancel`` and directly
    cancels ``run_id`` on the conversation session.
    """
    explicit = str(params.get("mission_id") or params.get("missionId") or "").strip()
    if explicit:
        return explicit
    if run_id:
        try:
            binding = db.get_team_mission_run_binding(run_id) or {}
        except Exception:
            binding = {}
        mid = str(binding.get("mission_id") or binding.get("missionId") or "").strip()
        if mid:
            return mid
    if mission and isinstance(mission, dict):
        mid = str(mission.get("mission_id") or "").strip()
        if mid:
            return mid
    if run_id:
        try:
            run = run_control.get_run(run_id, db=db) or {}
        except Exception:
            run = {}
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        mid = str(
            metadata.get("mission_id")
            or metadata.get("missionId")
            or run.get("mission_id")
            or run.get("missionId")
            or ""
        ).strip()
        if mid:
            return mid
    return ""


@method("team_mission.conversation.recall_turn")
def _(rid, params: dict) -> dict:
    """Recall a single user turn in a team conversation — the cross-participant
    counterpart of ``session.recall_turn``. Cascades cancellation for any
    derived execution the turn triggered:

      A) direct conversation run       → run.cancel(run_id on conv session)
      B) leader-triggered team mission → team_mission.cancel(mission_id)
                                         (this internally cancels every
                                         worker run + binding)

    Then defers to ``session.recall_turn`` on the conversation session to
    soft-delete (active=0) the user message and every subsequent assistant
    row up to the next user message — that's how mirrored member replies
    get retracted from the shared transcript (they live between two user
    turns; the by-position recall sweeps them up).

    Finally, mirrors the deactivation into every member-chat view session
    so a worker that ran (or will run) for this conversation no longer sees
    the retracted turn in its hydrated history.
    """
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    conversation_session_id = _conversation_session_id_from_params(params, {})
    conversation_id = _conversation_id_from_params(params, {})
    turn_id = str(params.get("turn_id") or params.get("turnId") or "").strip()
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    if not turn_id:
        return _err(rid, 4006, "turn_id required")

    optimistic_run_id = str(
        params.get("run_id") or params.get("runId") or params.get("client_run_id") or ""
    ).strip()
    reason = str(params.get("reason") or "").strip() or "Recalled by user."

    # PR-C moved member workers onto the conversation session and stopped
    # run_id + conversation_session_id, unless the run is bound to a mission.

    # Mission identity (only meaningful for B; ignored for A/C).
    resolved_mission = {}
    try:
        resolved = (
            db.resolve_team_mission_conversation(conversation_id)
            if conversation_id
            else {}
        )
        if isinstance(resolved, dict):
            resolved_mission = (
                resolved.get("mission")
                if isinstance(resolved.get("mission"), dict)
                else {}
            )
    except Exception:
        resolved_mission = {}
    mission_id = _recall_mission_id_from_run(
        db,
        run_id=optimistic_run_id,
        params=params,
        mission=resolved_mission,
    )

    cancelled_mission_ids: list[str] = []
    cancelled_run_ids: list[dict] = []
    cancel_errors: list[dict] = []

    # Step 1 — cascade cancel BEFORE we soft-delete messages, so any in-flight
    # event the worker is about to emit gets its terminal frame first.
    if mission_id:
        # B: leader-triggered mission. team_mission.cancel takes care of every
        # node + binding + worker run cascade — we don't reimplement that.
        try:
            response = _methods["team_mission.cancel"](
                rid,
                {
                    "mission_id": mission_id,
                    "canceled_by": "user",
                    "reason": reason,
                },
            )
        except Exception as exc:
            response = {"error": {"code": 5000, "message": str(exc)}}
        if isinstance(response, dict) and not response.get("error"):
            cancelled_mission_ids.append(mission_id)
            result_payload = (
                response.get("result")
                if isinstance(response.get("result"), dict)
                else {}
            )
            for run_info in result_payload.get("canceled_runs") or []:
                if isinstance(run_info, dict):
                    cancelled_run_ids.append(run_info)
            for err in result_payload.get("cancel_errors") or []:
                if isinstance(err, dict):
                    cancel_errors.append(err)
        elif isinstance(response, dict) and response.get("error"):
            cancel_errors.append({
                "mission_id": mission_id,
                "message": str(
                    response.get("error", {}).get("message")
                    or "team_mission.cancel failed"
                ),
            })
    if not mission_id and optimistic_run_id:
        # A: direct leader/member conversation run. PR-C makes the conversation
        # session the stored session for both surfaces, so no memberchat lookup
        # or fallback target is needed.
        try:
            cancel_resp = _methods["run.cancel"](
                rid,
                {
                    "run_id": optimistic_run_id,
                    "conversation_session_id": conversation_session_id,
                    "reason": reason,
                },
            )
        except Exception as exc:
            cancel_resp = {"error": {"code": 5000, "message": str(exc)}}
        if isinstance(cancel_resp, dict) and not cancel_resp.get("error"):
            cancelled_run_ids.append({
                "run_id": optimistic_run_id,
                "conversation_session_id": conversation_session_id,
                "status": "cancelled",
            })
        else:
            cancel_errors.append({
                "run_id": optimistic_run_id,
                "message": str(
                    (cancel_resp or {}).get("error", {}).get("message")
                    or "leader run.cancel failed"
                ),
            })

    # Step 2 — snapshot the message ids we're about to retract, BEFORE
    # session.recall_turn flips active=0, so we can mirror the retraction
    # into the member-chat view sessions.
    affected_source_ids = _recall_collect_conv_message_ids(
        db,
        conversation_session_id=conversation_session_id,
        turn_id=turn_id,
    )

    # Step 3 — actually retract on the conv session. session.recall_turn
    # handles soft-delete + interrupt + composer draft restoration; we just
    # pass-through its result and add our cascade summary on top.
    recall_params = {
        "session_id": conversation_session_id,
        "turn_id": turn_id,
        "mode": str(params.get("mode") or "restore_to_composer"),
    }
    client_message_id = str(
        params.get("client_message_id") or params.get("clientMessageId") or ""
    ).strip()
    if client_message_id:
        recall_params["client_message_id"] = client_message_id
    if optimistic_run_id:
        recall_params["run_id"] = optimistic_run_id
    try:
        recall_resp = _methods["session.recall_turn"](rid, recall_params)
    except Exception as exc:
        return _err(rid, 5000, f"conversation recall failed: {exc}")
    if isinstance(recall_resp, dict) and recall_resp.get("error"):
        return recall_resp
    recall_result = recall_resp.get("result") if isinstance(recall_resp, dict) else {}
    recall_result = recall_result if isinstance(recall_result, dict) else {}

    invalidated_context_ids: list[str] = []
    if affected_source_ids:
        try:
            invalidated_context_ids = (
                db.conversation_memory.invalidate_summaries_for_events(
                    [str(source_id) for source_id in affected_source_ids],
                    reason=f"conversation turn {turn_id} retracted",
                )
            )
        except Exception:
            _log.warning(
                "team conversation summary invalidation failed session=%s turn=%s",
                conversation_session_id,
                turn_id,
                exc_info=True,
            )

    return _ok(
        rid,
        {
            "status": "recalled",
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "turn_id": turn_id,
            "recalled": {
                # Preserve the authoritative conversation rewrite returned by
                # session.recall_turn. Recall is a deliberately non-monotonic
                # transcript mutation, so callers must be able to replace
                # their local projection immediately instead of depending on
                # eventual delivery of the session.recalled notification.
                **recall_result,
                "removed_messages": int(recall_result.get("removed_messages") or 0),
                "source_message_ids": affected_source_ids,
                "invalidated_context_ids": invalidated_context_ids,
                "interrupted": bool(recall_result.get("interrupted")),
                "draft": recall_result.get("draft") or {},
            },
            "cancelled": {
                "mission_ids": cancelled_mission_ids,
                "runs": cancelled_run_ids,
                "errors": cancel_errors,
            },
            "cascade_type": "B" if cancelled_mission_ids else "A",
        },
    )


def _resolve_cancel_mission_id(db, params: dict) -> str:
    def _existing_mission_id(candidate: str) -> str:
        candidate = str(candidate or "").strip()
        if not candidate:
            return ""
        graph = db.team_mission_graphs.get_team_mission_graph(candidate)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return ""
        return str(
            mission.get("mission_id") or mission.get("missionId") or candidate
        ).strip()

    explicit_mission_id = _mission_id_from_params(params)
    resolved = _existing_mission_id(explicit_mission_id)
    if resolved:
        return resolved

    identifiers = [
        _conversation_id_from_params(params, {}),
        _conversation_session_id_from_params(params, {}),
        explicit_mission_id,
    ]
    seen: set[str] = set()
    for identifier in identifiers:
        identifier = str(identifier or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        resolver = getattr(db, "resolve_team_mission_conversation", None)
        projection = resolver(identifier) if callable(resolver) else {}
        if not isinstance(projection, dict):
            continue
        conversation = (
            projection.get("conversation")
            if isinstance(projection.get("conversation"), dict)
            else {}
        )
        projection_mission = (
            projection.get("mission")
            if isinstance(projection.get("mission"), dict)
            else {}
        )
        graph = (
            projection.get("graph") if isinstance(projection.get("graph"), dict) else {}
        )
        graph_mission = (
            graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
        )
        for candidate in (
            conversation.get("active_mission_id"),
            conversation.get("activeMissionId"),
            projection_mission.get("mission_id"),
            projection_mission.get("missionId"),
            graph_mission.get("mission_id"),
            graph_mission.get("missionId"),
        ):
            resolved = _existing_mission_id(str(candidate or "").strip())
            if resolved:
                return resolved
    return ""


@method("team_mission.cancel")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    if not (
        _mission_id_from_params(params)
        or _conversation_id_from_params(params, {})
        or _conversation_session_id_from_params(params, {})
    ):
        return _err(rid, 4006, "mission_id or conversation_id required")
    mission_id = _resolve_cancel_mission_id(db, params)
    if not mission_id:
        return _err(rid, 4040, "team mission not found")
    reason = (
        str(params.get("reason") or "").strip() or "Team Mission cancelled by user."
    )
    result = db.cancel_team_mission(
        mission_id=mission_id,
        canceled_by=str(
            params.get("canceled_by") or params.get("canceledBy") or "user"
        ),
        reason=reason,
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    canceled_runs: list[dict] = []
    cancel_errors: list[dict] = []
    seen_run_ids: set[str] = set()
    for binding in result.get("cancel_run_bindings") or []:
        if not isinstance(binding, dict):
            continue
        run_id = str(binding.get("run_id") or "").strip()
        if not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        conversation_session_id = str(
            binding.get("session_id") or binding.get("conversation_session_id") or ""
        ).strip()
        cancel_params = {
            "run_id": run_id,
            "conversation_session_id": conversation_session_id,
            "execution_session_id": str(binding.get("execution_session_id") or ""),
            "runtime_scope_key": str(
                binding.get("runtime_scope_key") or conversation_session_id
            ),
            "reason": reason,
        }
        try:
            response = _methods["run.cancel"](rid, cancel_params)
        except Exception as exc:
            cancel_errors.append({"run_id": run_id, "message": str(exc)})
            continue
        if isinstance(response, dict) and response.get("error"):
            error = (
                response.get("error") if isinstance(response.get("error"), dict) else {}
            )
            cancel_errors.append({
                "run_id": run_id,
                "message": str(
                    error.get("message") or response.get("error") or "run cancel failed"
                ),
            })
            continue
        response_result = (
            response.get("result")
            if isinstance(response, dict) and isinstance(response.get("result"), dict)
            else {}
        )
        canceled_runs.append({
            "run_id": run_id,
            "conversation_session_id": conversation_session_id,
            "status": str(response_result.get("status") or "cancelled"),
            "turn_id": str(response_result.get("turn_id") or ""),
        })
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "mission_status": result.get("mission_status") or "cancelled",
            "canceled_nodes": list(result.get("canceled_nodes") or []),
            "canceled_runs": canceled_runs,
            "cancel_errors": cancel_errors,
            "graph": db.team_mission_graphs.get_team_mission_graph(mission_id),
        },
    )


@method("team_mission.node.bind_run")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    node_id = _node_id_from_params(params)
    run_id = _run_id_from_params(params)
    conversation_session_id = str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    if not run_id or not conversation_session_id:
        return _err(rid, 4006, "run_id and conversation_session_id required")
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return _err(rid, 4040, "team mission node not found")
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or node.get("runtime_scope_key")
        or conversation_session_id
    ).strip()
    binding = db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=conversation_session_id,
        execution_session_id=str(
            params.get("execution_session_id") or params.get("executionSessionId") or ""
        ),
        runtime_scope_key=runtime_scope_key,
        role=str(params.get("role") or node.get("kind") or "worker"),
        metadata={"source": "team_mission.node.bind_run"},
    )
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(node.get("kind") or "worker"),
        title=str(node.get("title") or ""),
        objective=str(node.get("objective") or ""),
        status=str(params.get("status") or "running"),
        assignee_profile_id=str(node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=runtime_scope_key,
        output_contract=dict(node.get("output_contract") or {}),
        metadata={
            **dict(node.get("metadata") or {}),
            "conversation_session_id": conversation_session_id,
            "run_id": run_id,
        },
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=run_id,
        event={
            "type": "mission.node.run.bound",
            "payload": {"node": node, "binding": binding},
        },
    )
    return _ok(rid, {"mission_id": mission_id, "node": node, "binding": binding})


@method("team_mission.node.start")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    node_id = _node_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    node_activity_id = f"act-node:{mission_id}:{node_id}"
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return _err(rid, 4040, "team mission node not found")
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = dict(node.get("metadata") or {})
    mission_metadata = (
        mission.get("metadata")
        if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict)
        else {}
    )
    conversation_id = _conversation_id_from_params(params, mission_metadata) or str(
        (mission or {}).get("conversation_id") or mission_id
    )
    conversation_session_id = _conversation_session_id_from_params(
        params, mission_metadata
    ) or _team_conversation_session_id(mission if isinstance(mission, dict) else {})
    visible_conversation_session_id = conversation_session_id
    if isinstance(mission, dict) and mission:
        db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            mission=mission,
        )
    if isinstance(mission, dict) and _is_root_planning_node(node):
        mission = _activate_mission_task(
            db, mission, node, source="team_mission.node.start"
        )
        graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    if (
        isinstance(mission, dict)
        and _is_root_planning_node(node)
        and not _falsey(
            params.get("record_user_task_message")
            if "record_user_task_message" in params
            else params.get("recordUserTaskMessage")
        )
    ):
        _append_team_user_task_message(
            db,
            mission=mission,
            objective=str(
                node.get("objective")
                or node.get("title")
                or mission.get("objective")
                or ""
            ),
            node_id=node_id,
            task_id=str(
                metadata.get("submitted_task_id") or metadata.get("task_id") or ""
            ),
        )
    conversation_session_id = str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or metadata.get("conversation_session_id")
        or _default_node_session_id(mission_id, node_id)
    ).strip()
    try:
        profile_params = _node_profile_params(
            params, mission if isinstance(mission, dict) else {}, node, db=db
        )
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or profile_params.get("runtime_scope_key")
        or node.get("runtime_scope_key")
        or conversation_session_id
    ).strip()
    run_id = str(
        params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex
    ).strip()
    turn_id = str(
        params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex
    ).strip()
    if not db.sessions.get(conversation_session_id):
        db.sessions.create(
            conversation_session_id, source="team_mission", transient=False
        )
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=mission if isinstance(mission, dict) else {},
            session_id=conversation_session_id,
            require=True,
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    bind_team_mission_session_workspace(
        session_id=conversation_session_id,
        context=workspace_context,
        metadata={
            "source": "team_mission.node.start",
            "conversation_id": conversation_id,
            "mission_id": mission_id,
            "node_id": node_id,
            "team_id": str((mission or {}).get("team_id") or ""),
        },
    )
    runtime_session_error = _ensure_team_mission_runtime_session_shell(
        conversation_session_id
    )
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    leader_control_node = _is_team_leader_control_node(node)
    base_text = _strategy_start_text(
        params, mission if isinstance(mission, dict) else {}, node
    )
    memory_context, memory_text = _team_memory_for_node(
        db,
        params,
        mission if isinstance(mission, dict) else {},
        node,
        objective=base_text,
    )
    worker_context: dict = {}
    if leader_control_node:
        text = f"{base_text}\n\n{memory_text}".strip() if memory_text else base_text
    else:
        worker_context = build_team_mission_worker_context(
            mission=mission if isinstance(mission, dict) else {},
            node=node,
            graph=graph if isinstance(graph, dict) else {},
            db=db,
            memory_context=memory_context if isinstance(memory_context, dict) else {},
            memory_text=memory_text,
        )
        text = str(worker_context.get("text") or base_text).strip()
    turn_system_context = text
    execution_input = str(
        node.get("objective")
        or node.get("title")
        or (mission or {}).get("objective")
        or (mission or {}).get("title")
        or "Execute the assigned team activity."
    ).strip()
    enabled_toolsets = _start_toolsets(
        params,
        mission if isinstance(mission, dict) else {},
        node,
        profile_params=profile_params,
        db=db,
    )
    agent_profile_id = str(
        profile_params.get("agent_profile_id")
        or params.get("agent_profile_id")
        or params.get("agentProfileId")
        or node.get("assignee_profile_id")
        or ""
    ).strip()
    agent_profile_version_id = str(
        profile_params.get("agent_profile_version_id")
        or params.get("agent_profile_version_id")
        or params.get("agentProfileVersionId")
        or node.get("assignee_profile_version_id")
        or ""
    ).strip()
    node_context_params = params
    if not _dovie_product_context_from_params(node_context_params):
        mission_dovie_context = mission_metadata.get(
            "dovie_product_context"
        ) or mission_metadata.get("dovieProductContext")
        if mission_dovie_context:
            node_context_params = {
                **params,
                "dovie_product_context": mission_dovie_context,
            }
    task_id = str(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
        or ""
    ).strip()
    mission_metadata = (
        mission.get("metadata")
        if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict)
        else {}
    )
    leader_members = (
        _leader_members_from_params(
            params, mission if isinstance(mission, dict) else {}, db=db
        )
        if leader_control_node
        else []
    )
    active_task = (
        dict(mission_metadata.get("active_task") or {})
        if isinstance(mission_metadata.get("active_task"), dict)
        else {}
    )
    if not active_task and _is_root_planning_node(node):
        active_task = {
            "task_id": task_id,
            "title": str(node.get("title") or mission.get("title") or "").strip(),
            "objective": str(
                node.get("objective") or mission.get("objective") or ""
            ).strip(),
            "root_node_id": node_id,
        }
    node_participant_id = (
        leader_participant_id(conversation_id)
        if leader_control_node
        else member_participant_id(
            str(
                node.get("member_id")
                or node.get("memberId")
                or metadata.get("member_id")
                or metadata.get("memberId")
                or node_id
            ).strip()
        )
    )
    activity_context_snapshot = _ensure_mission_activity_context_snapshot(
        db,
        mission=mission if isinstance(mission, dict) else {},
        conversation_session_id=visible_conversation_session_id,
        leader_participant=leader_participant_id(conversation_id),
    )
    activity_context_memory_ids = list(
        activity_context_snapshot.get("selected_memory_ids") or []
    )
    resume_summary_text = _activity_context_summary_text(
        db,
        activity_id=node_activity_id,
        node_id=node_id,
        attempt_id=run_id,
    )
    if resume_summary_text:
        turn_system_context = f"{turn_system_context}\n\n{resume_summary_text}".strip()
    run_context = RunContext(
        conversation_session_id=visible_conversation_session_id,
        participant_id=node_participant_id,
        activity_id=node_activity_id,
        activity_kind="mission",
        execution_scope_key=runtime_scope_key,
        control_home=_control_plane_home(),
        execution_home=_home_from_profile_params(profile_params),
        execution_session_id=conversation_session_id,
        node_id=node_id,
        attempt_id=run_id,
        activity_context_snapshot_id=str(
            activity_context_snapshot.get("snapshot_id") or ""
        ),
        **_actor_context_snapshot_fields(
            db,
            conversation_session_id=visible_conversation_session_id,
            participant_id=node_participant_id,
            execution_scope_key=runtime_scope_key,
            activity_id=node_activity_id,
            activity_kind="mission",
            profile_id=agent_profile_id,
            profile_version_id=agent_profile_version_id,
            node_id=node_id,
            attempt_id=run_id,
            selected_memory_ids=[
                *activity_context_memory_ids,
                *list((memory_context or {}).get("item_ids") or []),
            ],
        ),
    )
    binding_metadata = {
        "turn_id": turn_id,
        "source": "team_mission.node.start",
        "activity_id": node_activity_id,
        "activityId": node_activity_id,
    }
    if task_id:
        binding_metadata["task_id"] = task_id
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=conversation_session_id,
        execution_session_id="",
        runtime_scope_key=runtime_scope_key,
        role=_node_role(node),
        metadata={
            **binding_metadata,
            "prebound": True,
            "run_context_json": _run_context_json(run_context),
        },
    )
    submit_params = {
        **params,
        **profile_params,
        "conversation_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        "run_context_json": _run_context_json(run_context),
        "agent_profile_id": agent_profile_id,
        "agent_profile_version_id": agent_profile_version_id,
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        # Node identity, graph state, handoffs, memory, and execution policy
        # are trusted activity context. The user-role input carries only the
        # delegated objective that initiated this execution.
        "text": execution_input,
        "turn_system_context": turn_system_context,
        "user_message_persistence": "external",
        "enabled_toolsets": enabled_toolsets,
        **(
            {"disabled_toolsets": _leader_disabled_toolsets(params)}
            if leader_control_node
            else {}
        ),
        **(
            {"toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE}
            if leader_control_node or enabled_toolsets
            else {}
        ),
        "dovie_product_context": _team_dovie_product_context(
            node_context_params,
            team_mission={
                "kind": "leader_planning_node"
                if leader_control_node
                else "mission_node",
                "surface": "mission_node",
                "mission_id": mission_id,
                "conversation_id": conversation_id,
                "conversation_session_id": conversation_session_id,
                "parent_conversation_session_id": conversation_session_id,
                "workspace_id": workspace_context["workspace_id"],
                "workspace_path": workspace_context["workspace_path"],
                "node_id": node_id,
                "node_title": node.get("title") or "",
                "node_kind": node.get("kind") or "",
                "node_role": _node_role(node),
                "node_phase": _node_phase(node),
                "active_task": active_task,
                "task_brief": worker_context.get("task_brief")
                if worker_context
                else _node_task_brief(node),
                "output_contract": node.get("output_contract") or {},
                "memory": memory_context,
                "activity_context_snapshot": {
                    "snapshot_id": activity_context_snapshot.get("snapshot_id") or "",
                    "activity_context_revision": activity_context_snapshot.get(
                        "activity_context_revision"
                    )
                    or 0,
                    "conversation_revision": activity_context_snapshot.get(
                        "conversation_revision"
                    )
                    or 0,
                    "selected_memory_ids": activity_context_memory_ids,
                },
                **({"members": leader_members} if leader_members else {}),
                "worker_context": {
                    key: value
                    for key, value in (worker_context or {}).items()
                    if key not in {"text", "task_brief", "memory"}
                },
                "delegate_inherits_parent_tools": not leader_control_node,
                **(
                    {"tool_policy": _team_leader_tool_policy(surface="leader_node")}
                    if leader_control_node
                    else {}
                ),
            },
            executing_agent_profile_id=agent_profile_id,
            agent_role="team_leader" if leader_control_node else "team_member",
        ),
    }
    response = _submit_run_via_worker_with_response(rid, submit_params)
    if isinstance(response, dict) and response.get("error"):
        response_error = (
            response.get("error")
            if isinstance(response.get("error"), dict)
            else {"error": response.get("error")}
        )
        failure = classify_team_mission_failure("error", response_error)
        node = db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id=node_id,
            kind=str(node.get("kind") or "worker"),
            title=str(node.get("title") or ""),
            objective=str(node.get("objective") or ""),
            status="blocked",
            assignee_profile_id=str(node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(
                node.get("assignee_profile_version_id") or ""
            ),
            runtime_scope_key=runtime_scope_key,
            output_contract=dict(node.get("output_contract") or {}),
            metadata={
                **metadata,
                "conversation_session_id": conversation_session_id,
                "start_error": str(
                    response_error.get("message") or response_error.get("error") or ""
                ),
                "effective_toolsets": enabled_toolsets,
                **(
                    {
                        "start_error_reason_code": failure["reason_code"],
                        "start_error_recoverability": failure["recoverability"],
                    }
                    if failure
                    else {}
                ),
            },
            position_x=float(node.get("position_x") or 0),
            position_y=float(node.get("position_y") or 0),
        )
        return response
    result = response.get("result") if isinstance(response, dict) else {}
    result_run_id = str((result or {}).get("run_id") or run_id).strip()
    result_turn_id = str((result or {}).get("turn_id") or turn_id).strip()
    result_scope = str(
        (result or {}).get("runtime_scope_key") or runtime_scope_key
    ).strip()
    binding_metadata["turn_id"] = result_turn_id
    binding = db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=result_run_id,
        session_id=conversation_session_id,
        execution_session_id=str((result or {}).get("session_id") or ""),
        runtime_scope_key=result_scope,
        role=_node_role(node),
        metadata=binding_metadata,
    )
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(node.get("kind") or "worker"),
        title=str(node.get("title") or ""),
        objective=str(node.get("objective") or ""),
        status="running",
        assignee_profile_id=str(node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=result_scope,
        output_contract=dict(node.get("output_contract") or {}),
        metadata={
            **metadata,
            "conversation_session_id": conversation_session_id,
            "run_id": result_run_id,
            "turn_id": result_turn_id,
            "effective_toolsets": enabled_toolsets,
        },
        position_x=float(node.get("position_x") or 0),
        position_y=float(node.get("position_y") or 0),
    )
    if (
        isinstance(mission, dict)
        and str(mission.get("status") or "") == "draft"
        and str(node.get("kind") or "") == "root"
        and _node_phase(node) == "planning"
    ):
        db.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="planning",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata=dict(mission.get("metadata") or {}),
        )
    db.append_team_mission_run_event(
        mission_id=mission_id,
        run_id=result_run_id,
        event={
            "type": "mission.node.started",
            "payload": {"node": node, "binding": binding},
        },
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "node": node,
            "binding": binding,
            "run": result,
            "conversation_session_id": conversation_session_id,
        },
    )


@method("team_mission.schedule.ready")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = _schedule_ready_nodes(
        db=db,
        rid=rid,
        params=params,
        trigger="team_mission.schedule.ready",
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


# Export underscore-prefixed helpers for the compatibility facade.
__all__ = [name for name in globals() if not name.startswith("__")]
