# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import json
import os
from pathlib import Path

from .common import *
from .participant_autocreate import ensure_member_chat_participant
from hermes_state.profile_dir import resolve_default_agent_dir
from hermes_state_participants import leader_participant_id, member_participant_id
from hermes_team_mission.domain.run_context import RunContext
from tui_gateway.services.transcript_projector import conversation_user_message_id_for


def _team_chain_log(stage: str, **fields) -> None:
    try:
        _log.warning("[dovie-team-chain] %s %s", stage, json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str))
    except Exception:
        pass


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


def _target_member_id_from_params(params: dict) -> str:
    return str(params.get("target_member_id") or params.get("targetMemberId") or "").strip()


def _find_team_member_by_id(members: list[dict], member_id: str) -> dict:
    member_id = str(member_id or "").strip()
    for member in members or []:
        if not isinstance(member, dict):
            continue
        mid = str(member.get("member_id") or member.get("memberId") or member.get("id") or "").strip()
        if mid and mid == member_id:
            return member
    return {}


def _upsert_team_user_submission_message(
    db,
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
) -> dict:
    """Persist the user's visible team-conversation turn exactly once."""
    if not hasattr(db, "upsert_projected_conversation_message"):
        raise RuntimeError("SessionDB does not support projected conversation messages")
    conversation_message_id = conversation_user_message_id_for(
        session_id=conversation_session_id,
        turn_id=turn_id,
        run_id=run_id,
        client_message_id=client_message_id,
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
        "team_mission": team_metadata,
    }
    if client_message_id:
        metadata["client_message_id"] = client_message_id
    return db.upsert_projected_conversation_message(
        session_id=conversation_session_id,
        conversation_message_id=conversation_message_id,
        role="user",
        content=text,
        participant_id="",
        metadata=metadata,
        status="completed",
    )


def _submit_run_via_worker_with_response(rid, submit_params: dict) -> dict:
    """Route a ``run.submit`` for team mission (leader / node / member-
    chat) through the same ``primary_dispatch`` path that single-chat
    uses. Returns a JSON-RPC-shaped response so callers can keep
    their existing ``response = _methods["run.submit"](rid, params)``
    handling unchanged.

    Phase 7: unifies team mission run dispatch with single-chat
    architecture. The previous in-process ``_methods["run.submit"]``
    direct call ran the agent in the main sidecar process with the
    main gateway's HERMES_HOME — so member nodes loaded the main
    gateway's SOUL.md / skills / memories instead of their own. The
    legacy fix was to route through ``proxy_to_runtime`` (Phase 6
    deleted that). The new fix is to route through
    ``primary_dispatch`` which spawns the worker on the member's
    profile scope.

    Fallback: if no transport / loop is available (background mission
    scheduler etc.), fall through to ``_methods["run.submit"]`` —
    the in-process path WILL load the wrong identity but that's
    less broken than failing the run entirely. The diagnostic
    ``member-chat-proxy-fallback-in-process`` warning surfaces the
    fallback so we can spot any caller that should be routed."""
    _team_chain_log(
        "hermes-run-dispatch-start",
        run_id=str(submit_params.get("run_id") or submit_params.get("client_run_id") or ""),
        turn_id=str(submit_params.get("turn_id") or ""),
        stored_session_id=str(submit_params.get("stored_session_id") or submit_params.get("session_id") or ""),
        runtime_scope_key=str(submit_params.get("runtime_scope_key") or ""),
        agent_profile_id=str(submit_params.get("agent_profile_id") or ""),
        dovie_profile=submit_params.get("dovie_profile") if isinstance(submit_params.get("dovie_profile"), dict) else {},
        team_mission=(submit_params.get("dovie_product_context") or {}).get("team_mission")
        if isinstance(submit_params.get("dovie_product_context"), dict)
        else {},
    )
    proxied = _proxy_run_submit_via_worker(submit_params)
    if proxied.get("error"):
        _team_chain_log(
            "hermes-run-dispatch-error",
            run_id=str(submit_params.get("run_id") or submit_params.get("client_run_id") or ""),
            runtime_scope_key=str(submit_params.get("runtime_scope_key") or ""),
            error=proxied.get("error") or "",
        )
        return _err(rid, 5020, proxied["error"])
    if proxied.get("ok"):
        _team_chain_log(
            "hermes-run-dispatch-proxied",
            run_id=str(submit_params.get("run_id") or submit_params.get("client_run_id") or ""),
            turn_id=str(submit_params.get("turn_id") or ""),
            stored_session_id=str(submit_params.get("stored_session_id") or submit_params.get("session_id") or ""),
            runtime_scope_key=str(submit_params.get("runtime_scope_key") or ""),
            agent_profile_id=str(submit_params.get("agent_profile_id") or ""),
        )
        # primary_dispatch already acknowledged the request on the
        # transport with its own synthetic rid. Construct the
        # JSON-RPC envelope the original caller (with its own rid)
        # expects.
        return _ok(rid, {
            "status": "queued",
            "run_id": str(
                submit_params.get("run_id")
                or submit_params.get("client_run_id")
                or ""
            ),
            "turn_id": str(submit_params.get("turn_id") or ""),
            "stored_session_id": str(
                submit_params.get("stored_session_id")
                or submit_params.get("session_id")
                or ""
            ),
            "runtime_scope_key": str(submit_params.get("runtime_scope_key") or ""),
            "source": "primary-run-worker",
        })
    # Fallback: no transport (background) → in-process. Wrong env
    # but better than dropping the run.
    run_control._diagnostic_warning(  # noqa: SLF001
        "team-mission-run-proxy-fallback-in-process",
        stored_session_id=str(submit_params.get("stored_session_id") or ""),
        runtime_scope_key=str(submit_params.get("runtime_scope_key") or ""),
        reason=proxied.get("reason") or "",
    )
    _team_chain_log(
        "hermes-run-dispatch-fallback-in-process",
        run_id=str(submit_params.get("run_id") or submit_params.get("client_run_id") or ""),
        stored_session_id=str(submit_params.get("stored_session_id") or submit_params.get("session_id") or ""),
        runtime_scope_key=str(submit_params.get("runtime_scope_key") or ""),
        reason=proxied.get("reason") or "",
    )
    return _methods["run.submit"](rid, submit_params)


def _proxy_run_submit_via_worker(submit_params: dict) -> dict:
    """Send a run.submit through the new ``WorkerSupervisor`` so the
    worker spawns on the member's profile scope and executes the prompt
    with that scope's HERMES_HOME (not the main gateway's).

    Phase 6: the previous implementation used the deleted
    ``proxy_to_runtime`` legacy ws-bridge. The current
    ``primary_dispatch`` is the new equivalent — it handles the same
    JSON-RPC request shape, intercepts run.submit / prompt.submit
    when a scope is set, and routes through ``WorkerSupervisor``.
    Worker events still flow back through ``record_event``; RunContext keeps
    user-visible events addressed to the conversation session.

    Returns:
      {"ok": True}              — primary_dispatch claimed the request
      {"ok": False, "reason"}   — no transport / loop unavailable / no
                                  scope, caller falls back to the
                                  in-process dispatch
      {"error": "..."}          — dispatch raised; surface upward
    """
    import asyncio
    import uuid as _uuid
    from tui_gateway.server import current_transport
    from tui_gateway.services.worker_runtime import primary_dispatch

    transport = current_transport()
    if transport is None:
        return {"ok": False, "reason": "no_transport"}
    loop = getattr(transport, "_loop", None)
    if loop is None or not loop.is_running():
        return {"ok": False, "reason": "loop_not_running"}
    req = {
        "jsonrpc": "2.0",
        "id": f"member-chat:{_uuid.uuid4().hex}",
        "method": "run.submit",
        "params": dict(submit_params),
    }
    fut = asyncio.run_coroutine_threadsafe(primary_dispatch(req, transport), loop)
    try:
        ok = fut.result(timeout=30.0)
    except Exception as exc:
        return {"error": f"member-chat dispatch failed: {exc}"}
    return {"ok": bool(ok)}


def _clear_stuck_member_session_run(db, stored_session_id: str) -> None:
    """Release any non-terminal run left on the member session by a prior turn
    that did not close cleanly (no reaper guards a member-chat session). Keeps
    multi-turn from hitting 'session busy'."""
    import time as _time
    terminal = {"completed", "succeeded", "failed", "cancelled", "canceled", "interrupted"}
    try:
        with db._lock:
            rows = db._conn.execute(
                "SELECT run_id, status, runtime_scope_key, turn_id FROM runs WHERE session_id = ? ORDER BY rowid DESC LIMIT 5",
                (stored_session_id,),
            ).fetchall()
    except Exception:
        return
    for row in rows or []:
        run_id = str((row["run_id"] if hasattr(row, "keys") else row[0]) or "").strip()
        status = str((row["status"] if hasattr(row, "keys") else row[1]) or "").lower()
        if not run_id or status in terminal:
            continue
        try:
            db.upsert_run(
                run_id=run_id,
                session_id=stored_session_id,
                runtime_scope_key=str((row["runtime_scope_key"] if hasattr(row, "keys") else row[2]) or ""),
                turn_id=str((row["turn_id"] if hasattr(row, "keys") else row[3]) or ""),
                status="interrupted",
                completed_at=_time.time(),
                error="member chat run released before new turn",
            )
        except Exception:
            pass


def _submit_message_to_member(
    rid,
    params: dict,
    *,
    db,
    target_member_id: str,
    conversation_id: str,
    conversation_session_id: str,
    mission: dict,
    text: str,
) -> dict:
    """Group-chat: run the @-mentioned worker member as a PLAIN worker run with a
    clean member profile context (no team mission / node / binding / canvas), and
    relay its reply into the conversation session with member identity. The key
    correctness requirement is to pass the MEMBER's dovie_profile (with
    hermesHomePath) + member scope and to NOT inherit the frontend's leader scope
    — otherwise the worker spawns against the leader scope with no home and the
    agent never starts ('prompt worker terminal event did not close active run')."""
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    members = _leader_members_from_params(params, mission if isinstance(mission, dict) else {}, db=db)
    member = _find_team_member_by_id(members, target_member_id)
    if not member:
        return _err(rid, 4040, "team member not found")
    if str(member.get("role") or "").strip().lower() in {"lead", "leader"}:
        return _err(rid, 4006, "target member must be a worker, not the leader")
    profile_params = _profile_params_from_member(member)
    agent_profile_id = str(profile_params.get("agent_profile_id") or "").strip()
    dovie_profile = profile_params.get("dovie_profile") if isinstance(profile_params.get("dovie_profile"), dict) else {}
    hermes_home = str(dovie_profile.get("hermesHomePath") or dovie_profile.get("hermes_home_path") or "").strip()
    if not agent_profile_id or not hermes_home:
        return _err(rid, 4006, "target member profile is not runnable (missing profile home)")
    # Group-chat scope key is per-(conversation, member), NOT the member's
    # profile-default scope. Reason: in a team where multiple members share an
    # underlying agent profile (very common — e.g. all members on
    # `profile:agent-default`), a profile-scoped key reuses the SAME worker
    # process across members. When one member's run finishes, a subsequent
    # member's run preempts it on the same worker; the first run's terminal
    # frame can race in late and is rejected ("prompt worker terminal event
    # did not close active run"). A per-(conversation, member) scope key gives
    # each chat partner its own worker, like a real group chat.
    member_scope = f"member-chat:{conversation_id}:{target_member_id}"
    # Keep the member profile's runtime context in the dovie_profile payload so
    # the spawned worker still uses the member's hermes_home / model / toolsets
    # — only the routing key is fresh. CRITICAL: overwrite BOTH the camelCase
    # AND snake_case keys, because runtime_proxy._scope_from_params prefers
    # snake_case `runtime_scope_key` and otherwise inherits the member's
    # default profile scope (e.g. `profile:<id>`) — spawning a generic worker
    # against the wrong HERMES_HOME (the main dovie home, with its default
    # "Hermes Agent" SOUL), so the member's identity / memories / skills
    # never load.
    dovie_profile = {
        **dovie_profile,
        "runtimeScopeKey": member_scope,
        "runtime_scope_key": member_scope,
    }
    display_name = str(
        member.get("display_name")
        or member.get("displayName")
        or member.get("profile_name")
        or member.get("profileName")
        or member.get("name")
        or ""
    ).strip()
    display_avatar = str(
        member.get("avatar")
        or member.get("profile_avatar")
        or member.get("profileAvatar")
        or member.get("agent_profile_avatar")
        or member.get("agentProfileAvatar")
        or ""
    ).strip()
    ensure_member_chat_participant(
        db,
        conversation_session_id=conversation_session_id,
        member=member,
        member_id=target_member_id,
        source="team_mission.member_chat.start",
    )
    try:
        db.upsert_conversation_participant(
            conversation_session_id=conversation_session_id,
            participant_id=member_participant_id(target_member_id),
            role="member",
            member_id=target_member_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=str(profile_params.get("agent_profile_version_id") or ""),
            runtime_scope_key=member_scope,
            display_name=display_name,
            avatar=display_avatar,
        )
    except Exception as exc:
        _log.warning(
            "team_mission.member_chat.start participant enrichment skipped conversation_session_id=%s member_id=%s: %s",
            conversation_session_id,
            target_member_id,
            exc,
        )

    # STEP ORDER FIX (2026-06-27): The original code did
    #   1. append_message(conv_session, role=user, ...)   ← FK FAIL: conv session row doesn't exist yet
    #   2. create memberchat worker session
    #   3. sync_member_chat_conversation_view
    #   4. ensure_team_mission_conversation  ← THIS creates the conv session row in sessions table
    #
    # The user's @-mention message silently vanished because step 1 hit a
    # FOREIGN KEY constraint (conv session_id wasn't in sessions table yet)
    # and the except/pass swallowed it. Once leader subsequently sent a
    # message, the conv was correctly seeded (leader path ensures conv
    # before appending) and the leader transcript persisted — but the
    # @member exchange was already lost. From the user's perspective:
    # "@member 那一轮历史消失了".
    #
    # Fix: ensure the conv session row exists FIRST (workspace resolve +
    # ensure_team_mission_conversation), THEN append the user message.

    # 1. Resolve workspace context (needed by ensure_team_mission_conversation).
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=mission if isinstance(mission, dict) else {},
            session_id=conversation_session_id,
            require=True,
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))

    # 2. Ensure the team conversation row exists. Some older/partial
    # conversation creation paths only materialize session_index, so the
    # canonical sessions row is enforced explicitly below before transcript
    # writes touch messages.session_id.
    conversation_title = _conversation_title_from_submit(db, params, text)
    team_id_for_ensure = str(
        params.get("team_id") or params.get("teamId")
        or (mission or {}).get("team_id") or (mission or {}).get("teamId")
        or ""
    ).strip()
    try:
        ensured_conversation = db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            stable_session_id=conversation_session_id,
            mission=mission if isinstance(mission, dict) and mission else {},
            mission_id=str((mission or {}).get("mission_id") or "") if isinstance(mission, dict) and mission else "",
            team_id=team_id_for_ensure,
            title=conversation_title,
            objective=text,
            workspace_id=workspace_context.get("workspace_id") if isinstance(workspace_context, dict) else "",
            workspace_path=workspace_context.get("workspace_path") if isinstance(workspace_context, dict) else "",
            created_by_user_id=str(params.get("created_by_user_id") or params.get("createdByUserId") or "").strip(),
            metadata={"display_title_source": "first_user_message"} if conversation_title else None,
        )
    except Exception as exc:
        return _err(rid, 5008, f"team conversation session unavailable: {exc}")
    try:
        _ensure_team_conversation_session(db, conversation_session_id)
    except Exception as exc:
        return _err(rid, 5008, f"team conversation session unavailable: {exc}")

    # 3. Reserve the run/turn identity before writing the user transcript row.
    # The same identity is sent to the worker, so retries/upserts cannot create
    # duplicate user messages and repeated text in later turns remains distinct.
    optimistic_run_id = str(params.get("client_run_id") or params.get("run_id") or "").strip()
    run_id = optimistic_run_id or uuid.uuid4().hex
    turn_id = str(params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex).strip()
    client_message_id = str(params.get("client_message_id") or params.get("clientMessageId") or "").strip()

    # 4. NOW it is safe to record the user's @-message into the shared
    # conversation transcript. The conv session row exists, FK satisfied. Use
    # an idempotent projected row; the worker owns execution, not visible user
    # transcript persistence.
    try:
        _upsert_team_user_submission_message(
            db,
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            turn_id=turn_id,
            text=text,
            target_member_id=target_member_id,
            display_name=display_name,
            client_message_id=client_message_id,
            source_kind="member_chat_user",
        )
    except Exception as exc:
        return _err(rid, 5008, f"team user message persistence failed: {exc}")

    member_run_home = _home_from_dovie_profile(dovie_profile)
    control_home = _control_plane_home()
    run_context = RunContext(
        conversation_session_id=conversation_session_id,
        participant_id=member_participant_id(target_member_id),
        activity_id="member_chat",
        activity_kind="member_chat",
        execution_scope_key=member_scope,
        control_home=control_home,
        execution_home=member_run_home,
    )

    runtime_session_error = _ensure_team_mission_runtime_session_shell(conversation_session_id)
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    if not isinstance(ensured_conversation, dict):
        ensured_conversation = {}

    # 5. The worker now runs on the conversation session itself. With no
    # memberchat mirror registry to rewrite run ids, use the frontend's
    # pre-reserved optimistic run id directly when present so terminal frames
    # settle the same conversation-side run the UI is tracking.

    # 6. run.submit with CLEAN member params only — NOT {**params} (which carries
    #    the frontend's leader scope/profile and breaks the worker spawn).
    submit_params = {
        "stored_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "agent_profile_id": agent_profile_id,
        "agent_profile_version_id": str(profile_params.get("agent_profile_version_id") or ""),
        "runtime_scope_key": member_scope,
        "run_context_json": _run_context_json(run_context),
        "dovie_profile": dovie_profile,
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        # Plain user text — the worker appends it as a real user turn on the
        # conversation session it now runs on. No prompt stringification.
        "text": text,
        "persist_user_message": "",
        "tool_progress_mode": "all",
        "cols": 120,
        "dovie_product_context": {
            "team_mission": {
                "kind": "member_chat",
                "surface": "member_chat",
                "conversation_id": conversation_id,
                "conversation_session_id": conversation_session_id,
                "member_id": target_member_id,
            },
        },
    }
    # Dispatch run.submit through the runtime-proxy path so the worker spawns
    # on the member-chat execution scope and runs inside the member's profile home
    # (HERMES_HOME=profiles/<member>). The in-process `_methods["run.submit"]`
    # path skips dispatch_method's `should_proxy_to_runtime` check, which is
    # why earlier attempts ran the prompt against the MAIN gateway's home —
    # the member's SOUL.md / memories / skills never loaded and every member
    # answered with the default "Hermes Agent" persona.
    proxied = _proxy_run_submit_via_worker(submit_params)
    if proxied.get("error"):
        return _err(rid, 5020, proxied["error"])
    if not proxied.get("ok"):
        # No transport available (e.g. stdio gateway path) — fall back to the
        # in-process path. Members will still answer, but with the main
        # gateway's default identity instead of their own. Surfaced loudly so
        # we can catch any caller still on the old path.
        run_control._diagnostic_warning(  # noqa: SLF001
            "member-chat-proxy-fallback-in-process",
            stored_session_id=conversation_session_id,
            member_scope=member_scope,
            reason=proxied.get("reason") or "",
        )
        response = _methods["run.submit"](rid, submit_params)
        if isinstance(response, dict) and response.get("error"):
            return response
    # Dispatched async via the proxy path — synthesize a turn descriptor that
    # matches what the in-process path used to return (run_id, turn_id, etc.)
    # so the frontend's optimistic UI has the same payload shape.
    conversation_run_id = run_id
    member_turn = {
        # Conversation-side identity: the worker now publishes directly to
        # the conversation session via RunContext, so source and visible run
        # ids are the same.
        "run_id": conversation_run_id,
        "worker_run_id": run_id,
        "source_run_id": run_id,
        "turn_id": turn_id,
        "stored_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "worker_stored_session_id": conversation_session_id,
        "runtime_scope_key": member_scope,
        "status": "streaming",
    }
    # Use the conversation we just ensured (guaranteed to carry canonical
    # fields). For graph, fall back to the read model — it may be empty for
    # a brand-new conv with no mission, but that's fine: the frontend
    # accepts an empty graph here, only the conversation payload is
    # canonical-checked.
    resolved = db.resolve_team_mission_conversation(conversation_id) if conversation_id else {}
    resolved = resolved if isinstance(resolved, dict) else {}
    return _ok(rid, {
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversation": ensured_conversation or resolved.get("conversation") or {},
        "graph": resolved.get("graph") or {},
        "leader_turn": member_turn,
        "member_turn": member_turn,
        "target_member_id": target_member_id,
    })


@method("team_mission.message.submit")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    explicit_mission_request = bool(mission_id)
    text = _message_text_from_params(params)
    if not text:
        return _err(rid, 4006, "text required")
    conversation_id = _conversation_id_from_params(params, {})
    conversation_session_id = _conversation_session_id_from_params(params, {})
    _team_chain_log(
        "hermes-message-submit-entry",
        mission_id=mission_id,
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        team_id=str(params.get("team_id") or params.get("teamId") or ""),
        target_member_id=_target_member_id_from_params(params),
        run_id=str(params.get("run_id") or params.get("client_run_id") or ""),
        turn_id=str(params.get("turn_id") or params.get("turnId") or ""),
        text_length=len(text),
        attachment_count=len(_submitted_attachments(params)),
        has_dovie_product_context=isinstance(params.get("dovie_product_context"), dict),
    )
    if not mission_id and not conversation_id:
        return _err(rid, 4006, "mission_id or conversation_id required")
    graph = db.get_team_mission_graph(mission_id) if mission_id else {}
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if mission_id and (not isinstance(mission, dict) or not mission):
        if not conversation_id:
            return _err(rid, 4040, "team mission not found")
        mission_id = ""
        explicit_mission_request = False
        graph = {}
        mission = {}
    conversation = {}
    context_graph = graph if isinstance(graph, dict) else {}
    context_mission = mission if isinstance(mission, dict) else {}
    if not mission:
        resolved_identifier = conversation_id
        resolved = db.resolve_team_mission_conversation(resolved_identifier) if resolved_identifier else {}
        if isinstance(resolved, dict):
            conversation = resolved.get("conversation") if isinstance(resolved.get("conversation"), dict) else {}
            resolved_graph = resolved.get("graph") if isinstance(resolved.get("graph"), dict) else {}
            resolved_mission = resolved.get("mission") if isinstance(resolved.get("mission"), dict) else {}
            if resolved_mission:
                context_mission = resolved_mission
                context_graph = resolved_graph
                if explicit_mission_request:
                    mission = resolved_mission
                    graph = resolved_graph
                    mission_id = str(mission.get("mission_id") or "").strip()
    identity_mission = mission if isinstance(mission, dict) and mission else context_mission
    metadata = identity_mission.get("metadata") if isinstance(identity_mission, dict) and isinstance(identity_mission.get("metadata"), dict) else {}
    conversation_id = (
        conversation_id
        or str((conversation or {}).get("conversation_id") or "").strip()
        or str((mission or {}).get("conversation_id") or "").strip()
        or mission_id
    )
    if not conversation_id:
        return _err(rid, 4006, "conversation_id required")
    conversation_session_id = (
        conversation_session_id
        or str((conversation or {}).get("stable_session_id") or "").strip()
        or (_team_conversation_session_id(mission) if mission else "")
    )
    if not conversation_session_id:
        return _err(rid, 4006, "conversation_session_id required")
    params = {
        **params,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversationSessionId": conversation_session_id,
        **({"mission_id": mission_id, "missionId": mission_id} if mission_id else {}),
    }
    archived_team_error = _archived_team_write_error(
        db,
        _team_id_for_profile(params, mission=identity_mission if isinstance(identity_mission, dict) else {}, conversation=conversation),
    )
    if archived_team_error:
        return _err(rid, 4023, archived_team_error)
    # Group-chat: route directly to a worker member, bypassing the leader.
    target_member_id = _target_member_id_from_params(params)
    if target_member_id:
        _team_chain_log(
            "hermes-message-submit-route-member",
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            target_member_id=target_member_id,
            run_id=str(params.get("run_id") or params.get("client_run_id") or ""),
            turn_id=str(params.get("turn_id") or params.get("turnId") or ""),
            runtime_scope_key=str(params.get("runtime_scope_key") or ""),
            mission_id=str((identity_mission or {}).get("mission_id") or mission_id or "") if isinstance(identity_mission, dict) else str(mission_id or ""),
        )
        return _submit_message_to_member(
            rid,
            params,
            db=db,
            target_member_id=target_member_id,
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            mission=identity_mission if isinstance(identity_mission, dict) else {},
            text=text,
        )
    try:
        params, leader_runtime_context = _resolve_team_leader_runtime_params_for_request(
            params,
            (graph if isinstance(graph, dict) and graph else context_graph) if isinstance(context_graph, dict) else {},
            db,
        )
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    prompt_graph = (graph if isinstance(graph, dict) and graph else context_graph) if isinstance(context_graph, dict) else {}
    profile_params = _leader_profile_params(params, prompt_graph if isinstance(prompt_graph, dict) else {})
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
            mission=identity_mission if isinstance(identity_mission, dict) else {},
            conversation=conversation if isinstance(conversation, dict) else {},
            session_id=conversation_session_id,
            require=True,
        )
        conversation_title = _conversation_title_from_submit(db, params, text)
        conversation = db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            stable_session_id=conversation_session_id,
            mission=mission if isinstance(mission, dict) and mission else {},
            mission_id=mission_id if isinstance(mission, dict) and mission else "",
            team_id=str(params.get("team_id") or params.get("teamId") or (identity_mission or {}).get("team_id") or ""),
            title=conversation_title,
            objective=str(params.get("objective") or params.get("prompt") or (identity_mission or {}).get("objective") or text),
            workspace_id=workspace_context["workspace_id"],
            workspace_path=workspace_context["workspace_path"],
            created_by_user_id=str(params.get("created_by_user_id") or params.get("createdByUserId") or (identity_mission or {}).get("created_by_user_id") or ""),
            metadata={"display_title_source": "first_user_message"} if conversation_title else None,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.message.submit",
                "conversation_id": conversation_id,
                **({"mission_id": mission_id} if mission_id else {}),
                "team_id": str(params.get("team_id") or params.get("teamId") or (identity_mission or {}).get("team_id") or ""),
            },
        )
        _ensure_team_conversation_session(db, conversation_session_id)
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team conversation session unavailable: {exc}")
    if not graph:
        graph = {
            "mission": mission if isinstance(mission, dict) else {},
            "conversation": conversation,
            "nodes": [],
            "edges": [],
            "run_bindings": [],
        }
    memory_context, memory_text = (
        _team_memory_for_leader_message(db, params, mission, objective=text)
        if isinstance(mission, dict) and mission
        else ({}, "")
    )
    run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    turn_id = str(params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex).strip()
    client_message_id = str(params.get("client_message_id") or params.get("clientMessageId") or "").strip()
    draft_text = str(params.get("draft_text") or params.get("draftText") or text)
    submitted_attachments = _submitted_attachments(params)
    direct_reply = _leader_message_requests_direct_reply(text)
    team_context = {
        "kind": "leader_conversation",
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "team_id": str(params.get("team_id") or params.get("teamId") or (identity_mission or {}).get("team_id") or (conversation or {}).get("team_id") or ""),
        "mode": (mission or {}).get("mode") or str(params.get("mode") or ""),
        "status": (mission or {}).get("status") or "",
        "workspace_id": workspace_context["workspace_id"],
        "workspace_path": workspace_context["workspace_path"],
        "memory": memory_context,
        "members": _leader_members_from_params(params, mission if isinstance(mission, dict) else {}),
        "tool_policy": _team_leader_tool_policy(surface="leader_conversation"),
    }
    snapshot_id = _team_capability_snapshot_id(params)
    if snapshot_id:
        team_context["team_capability_snapshot_id"] = snapshot_id
    if mission_id:
        team_context["mission_id"] = mission_id
    activity_mission_id = str(
        (identity_mission or {}).get("mission_id")
        or (identity_mission or {}).get("missionId")
        or mission_id
        or ""
    ).strip() if isinstance(identity_mission, dict) else str(mission_id or "").strip()
    leader_activity_kind = "mission" if activity_mission_id else "chat"
    leader_run_home = _home_from_profile_params(profile_params)
    control_home = _control_plane_home()
    run_context = RunContext(
        conversation_session_id=conversation_session_id,
        participant_id=leader_participant_id(conversation_id),
        activity_id=activity_mission_id or "chat",
        activity_kind=leader_activity_kind,
        execution_scope_key=runtime_scope_key,
        control_home=control_home,
        execution_home=leader_run_home,
    )
    _team_chain_log(
        "hermes-message-submit-route-leader",
        mission_id=mission_id,
        activity_mission_id=activity_mission_id,
        activity_kind=leader_activity_kind,
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        run_id=run_id,
        turn_id=turn_id,
        runtime_scope_key=runtime_scope_key,
        agent_profile_id=str(profile_params.get("agent_profile_id") or ""),
        profile_runtime_scope_key=str(profile_params.get("runtime_scope_key") or ""),
        hermes_home=_home_from_profile_params(profile_params),
        direct_reply=direct_reply,
        toolsets=[] if direct_reply else _leader_message_toolsets(params),
        disabled_toolsets=_leader_disabled_toolsets(params),
    )
    try:
        _upsert_team_user_submission_message(
            db,
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            turn_id=turn_id,
            text=draft_text,
            client_message_id=client_message_id,
            source_kind="leader_chat_user",
        )
    except Exception as exc:
        return _err(rid, 5008, f"team user message persistence failed: {exc}")
    submit_params = {
        **params,
        **profile_params,
        "stored_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        "run_context_json": _run_context_json(run_context),
        "agent_context_mode": "team_leader",
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        "text": (
            _leader_direct_reply_prompt(
                user_text=text,
                graph=prompt_graph if isinstance(prompt_graph, dict) and prompt_graph else graph,
                memory_text=memory_text,
            )
            if direct_reply
            else _leader_router_prompt(user_text=text, graph=prompt_graph if isinstance(prompt_graph, dict) and prompt_graph else graph, memory_text=memory_text)
        ),
        "persist_user_message": "",
        "draft_text": draft_text,
        "attachments": submitted_attachments,
        "enabled_toolsets": [] if direct_reply else _leader_message_toolsets(params),
        "disabled_toolsets": _leader_disabled_toolsets(params),
        "toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE,
        "dovie_product_context": {
            **(params.get("dovie_product_context") if isinstance(params.get("dovie_product_context"), dict) else {}),
            "team_mission": team_context,
        },
    }
    if direct_reply:
        submit_params["reasoning_config"] = dict(_TEAM_LEADER_DIRECT_REPLY_REASONING_CONFIG)
    runtime_session_error = _ensure_team_mission_runtime_session_shell(conversation_session_id)
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    response = _submit_run_via_worker_with_response(rid, submit_params)
    if isinstance(response, dict) and response.get("error"):
        _team_chain_log(
            "hermes-message-submit-run-error",
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            turn_id=turn_id,
            runtime_scope_key=runtime_scope_key,
            error=response.get("error"),
        )
        return response
    result = response.get("result") if isinstance(response, dict) else {}
    _team_chain_log(
        "hermes-message-submit-run-returned",
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        run_id=str(result.get("run_id") or run_id),
        turn_id=str(result.get("turn_id") or turn_id),
        runtime_session_id=str(result.get("session_id") or ""),
        stored_session_id=str(result.get("stored_session_id") or ""),
        runtime_scope_key=str(result.get("runtime_scope_key") or runtime_scope_key),
        status=str(result.get("status") or ""),
    )
    if isinstance(mission, dict) and mission and submitted_attachments:
        _record_leader_input_attachment_artifacts(
            db,
            mission=mission,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            attachments=submitted_attachments,
        )
    ensure_team_leader_message_run_state(db, run_id=run_id, session_id=conversation_session_id, runtime_scope_key=runtime_scope_key, result=result)
    _team_chain_log(
        "hermes-message-submit-return",
        mission_id=mission_id,
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        run_id=str(result.get("run_id") or run_id),
        turn_id=str(result.get("turn_id") or turn_id),
        runtime_session_id=str(result.get("session_id") or ""),
        runtime_scope_key=str(result.get("runtime_scope_key") or runtime_scope_key),
        graph_node_count=len((graph or {}).get("nodes") or []) if isinstance(graph, dict) else 0,
        activity_mission_id=activity_mission_id,
        direct_reply=direct_reply,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "conversation_id": conversation_id,
            "conversation_session_id": conversation_session_id,
            "conversation": conversation,
            "leader_turn": result or {},
            "leader_runtime_context": leader_runtime_context,
            "leaderRuntimeContext": leader_runtime_context,
            "graph": db.get_team_mission_graph(mission_id) if mission_id else graph,
        },
    )


@method("team_mission.graph")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    conversation_id = _conversation_id_from_params(params)
    if conversation_id:
        graph = db.get_team_mission_conversation_graph(conversation_id)
        if not graph:
            return _err(rid, 4040, "team mission conversation not found")
        mission_ids = _graph_mission_ids(graph)
        if mission_id and mission_id not in mission_ids:
            return _err(rid, 4040, "team mission not found in conversation")
        graph_mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
        graph_mission_id = str(
            graph_mission.get("mission_id")
            or graph_mission.get("missionId")
            or ""
        ).strip()
        result = {
            "mission_id": graph_mission_id or mission_id,
            "conversation_id": conversation_id,
            "graph": graph,
        }
        if mission_id and mission_id != result["mission_id"]:
            result["requested_mission_id"] = mission_id
        return _ok(rid, result)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    graph = db.get_team_mission_graph(mission_id)
    if not graph:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, {"mission_id": mission_id, "graph": graph})


@method("team_mission.graph.reduce")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = db.reduce_team_mission_graph(mission_id)
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, result)


@method("team_mission.events")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    limit = _bounded_limit(params.get("limit"), default=2000, maximum=10000)
    byte_limit = _bounded_byte_limit(params.get("byte_limit") or params.get("byteLimit"))
    raw_events = db.list_team_mission_events(
        mission_id,
        after_seq=after_seq,
        limit=min(limit + 1, 10000),
    )
    events, has_more, approx_event_bytes = _team_mission_event_page(
        raw_events,
        after_seq=after_seq,
        limit=limit,
        byte_limit=byte_limit,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "audit_only": True,
            "auditOnly": True,
            "events": events,
            "last_event_seq": max([int(event.get("seq") or 0) for event in events], default=after_seq),
            "has_more": has_more,
            "approx_event_bytes": approx_event_bytes,
        },
    )


@method("team_mission.subscribe")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    # Stale-run watchdog: clear any run left active under an already-terminal
    # mission (cancel race / completion without a node terminal event / a run
    # that stalled on a live gateway). Self-heals the spinning card + lingering
    # runtime state when the conversation is opened/refreshed.
    reaper = getattr(db, "reap_terminal_mission_runs", None)
    if callable(reaper):
        try:
            reaper(mission_id)
        except Exception:
            pass
    # Cap canonical-log growth: prune per-token stream deltas of a terminal
    # mission (unbounded team_mission_events bloated the DB into the GBs, slowing
    # session-list loads into timeouts). Lazy, idempotent, terminal-only.
    pruner = getattr(db, "prune_team_mission_events", None)
    if callable(pruner):
        try:
            pruner(mission_id)
        except Exception:
            pass
    try:
        after_seq = int(params.get("after_seq") or params.get("afterSeq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    limit = _bounded_limit(params.get("limit"), default=2000, maximum=10000)
    byte_limit = _bounded_byte_limit(params.get("byte_limit") or params.get("byteLimit"))
    subscription_id, raw_events = run_control.subscribe_team_mission_with_id(
        mission_id=mission_id,
        transport=current_transport(),
        after_seq=after_seq,
        limit=min(limit + 1, 10000),
        db=db,
    )
    events, has_more, approx_event_bytes = _team_mission_event_page(
        raw_events,
        after_seq=after_seq,
        limit=limit,
        byte_limit=byte_limit,
    )
    last_event_seq = max([int(event.get("seq") or 0) for event in events], default=after_seq)
    _log.info(
        "team_mission.subscribe replay mission_id=%s after_seq=%s limit=%s byte_limit=%s subscription_id=%s raw_count=%s replay_count=%s last_seq=%s has_more=%s approx_event_bytes=%s",
        mission_id,
        after_seq,
        limit,
        byte_limit,
        subscription_id,
        len(raw_events),
        len(events),
        last_event_seq,
        has_more,
        approx_event_bytes,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "subscription_id": subscription_id,
            "audit_only": True,
            "auditOnly": True,
            "events": events,
            "last_event_seq": last_event_seq,
            "has_more": has_more,
            "approx_event_bytes": approx_event_bytes,
        },
    )


@method("team_mission.node.create")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    payload = _node_payload_from_params(params)
    node_id = str(payload.get("node_id") or payload.get("nodeId") or payload.get("id") or "").strip()
    if not node_id:
        return _err(rid, 4006, "node_id required")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return _err(rid, 4040, "team mission not found")
    metadata = payload.get("metadata") or {}
    output_contract = payload.get("output_contract") or payload.get("outputContract") or {}
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "node.metadata must be an object")
    if not isinstance(output_contract, dict):
        return _err(rid, 4004, "node.output_contract must be an object")
    metadata = dict(metadata)
    assignee_member_id = str(payload.get("assignee_member_id") or payload.get("assigneeMemberId") or "").strip()
    assignee_role = str(payload.get("assignee_role") or payload.get("assigneeRole") or "").strip()
    if assignee_member_id:
        metadata.setdefault("assignee_member_id", assignee_member_id)
    if assignee_role:
        metadata.setdefault("assignee_role", assignee_role)
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(payload.get("kind") or "worker"),
        title=str(payload.get("title") or ""),
        objective=str(payload.get("objective") or ""),
        status=str(payload.get("status") or "ready"),
        assignee_profile_id=str(payload.get("assignee_profile_id") or payload.get("assigneeProfileId") or ""),
        assignee_profile_version_id=str(
            payload.get("assignee_profile_version_id") or payload.get("assigneeProfileVersionId") or ""
        ),
        runtime_scope_key=str(payload.get("runtime_scope_key") or payload.get("runtimeScopeKey") or ""),
        output_contract=output_contract,
        metadata=metadata,
        position_x=float(payload.get("position_x") or payload.get("x") or 0),
        position_y=float(payload.get("position_y") or payload.get("y") or 0),
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.node.created", "payload": {"node": node}},
        )
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    node_metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    if isinstance(mission, dict) and _is_root_planning_node(node):
        mission = _activate_mission_task(db, mission, node, source="team_mission.node.create")
        graph = db.get_team_mission_graph(mission_id)
    if (
        isinstance(mission, dict)
        and _is_root_planning_node(node)
        and not _falsey(params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"))
    ):
        _append_team_user_task_message(
            db,
            mission=mission,
            objective=str(node.get("objective") or node.get("title") or ""),
            node_id=str(node.get("node_id") or ""),
            task_id=str(node_metadata.get("submitted_task_id") or node_metadata.get("task_id") or ""),
        )
    return _ok(rid, {"mission_id": mission_id, "node": node, "graph": graph})


@method("team_mission.edge.create")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    payload = _edge_payload_from_params(params)
    from_node_id = str(payload.get("from_node_id") or payload.get("fromNodeId") or payload.get("source") or "").strip()
    to_node_id = str(payload.get("to_node_id") or payload.get("toNodeId") or payload.get("target") or "").strip()
    if not from_node_id or not to_node_id:
        return _err(rid, 4006, "from_node_id and to_node_id required")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "edge.metadata must be an object")
    edge = db.upsert_team_mission_edge(
        mission_id=mission_id,
        edge_id=str(payload.get("edge_id") or payload.get("edgeId") or payload.get("id") or ""),
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        kind=str(payload.get("kind") or "depends_on"),
        metadata=metadata,
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.edge.created", "payload": {"edge": edge}},
        )
    return _ok(rid, {"mission_id": mission_id, "edge": edge, "graph": db.get_team_mission_graph(mission_id)})


@method("team_mission.node.update")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    payload = _node_payload_from_params(params)
    node_id = str(payload.get("node_id") or payload.get("nodeId") or payload.get("id") or "").strip()
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    existing = db.get_team_mission_node(mission_id, node_id)
    if not existing:
        return _err(rid, 4040, "team mission node not found")
    metadata = dict(existing.get("metadata") or {})
    if isinstance(payload.get("metadata"), dict):
        metadata.update(payload.get("metadata") or {})
    assignee_member_id = str(payload.get("assignee_member_id") or payload.get("assigneeMemberId") or "").strip()
    assignee_role = str(payload.get("assignee_role") or payload.get("assigneeRole") or "").strip()
    if assignee_member_id:
        metadata.setdefault("assignee_member_id", assignee_member_id)
    if assignee_role:
        metadata.setdefault("assignee_role", assignee_role)
    output_contract = dict(existing.get("output_contract") or {})
    if isinstance(payload.get("output_contract") or payload.get("outputContract"), dict):
        output_contract.update(payload.get("output_contract") or payload.get("outputContract") or {})
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(payload.get("kind") or existing.get("kind") or "worker"),
        title=str(payload.get("title") or existing.get("title") or ""),
        objective=str(payload.get("objective") or existing.get("objective") or ""),
        status=str(payload.get("status") or existing.get("status") or "todo"),
        assignee_profile_id=str(
            payload.get("assignee_profile_id") or payload.get("assigneeProfileId") or existing.get("assignee_profile_id") or ""
        ),
        assignee_profile_version_id=str(
            payload.get("assignee_profile_version_id")
            or payload.get("assigneeProfileVersionId")
            or existing.get("assignee_profile_version_id")
            or ""
        ),
        runtime_scope_key=str(payload.get("runtime_scope_key") or payload.get("runtimeScopeKey") or existing.get("runtime_scope_key") or ""),
        output_contract=output_contract,
        metadata=metadata,
        position_x=float(payload.get("position_x") or payload.get("x") or existing.get("position_x") or 0),
        position_y=float(payload.get("position_y") or payload.get("y") or existing.get("position_y") or 0),
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.node.updated", "payload": {"node": node}},
        )
    schedule_result = {}
    if str(node.get("status") or "") in {"completed", "verified"}:
        schedule_result = _schedule_ready_nodes(
            db=db,
            rid=rid,
            params={"mission_id": mission_id},
            trigger="node.update",
        )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "node": node,
            "scheduled": schedule_result,
            "graph": (schedule_result.get("graph") if isinstance(schedule_result, dict) else None) or db.get_team_mission_graph(mission_id),
        },
    )


@method("team_mission.plan.complete")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else None
    if not isinstance(mission, dict):
        return _err(rid, 4040, "team mission not found")
    result = db.complete_team_mission_plan(
        mission_id=mission_id,
        run_id=_run_id_from_params(params),
        task_id=str(params.get("task_id") or params.get("taskId") or ""),
        event_source="plan.complete",
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    schedule_result = {}
    if bool(result.get("auto_start_ready_nodes")):
        schedule_result = _schedule_ready_nodes(
            db=db,
            rid=rid,
            params={"mission_id": mission_id},
            trigger="plan.complete",
        )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "mission_status": result.get("mission_status") or "",
            "approval_requests": list(result.get("approval_requests") or []),
            "auto_start_ready_nodes": bool(result.get("auto_start_ready_nodes")),
            "scheduled": schedule_result,
            "graph": (schedule_result.get("graph") if isinstance(schedule_result, dict) else None) or result.get("graph") or {},
        },
    )


@method("team_mission.plan.approve")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else None
    if not isinstance(mission, dict):
        return _err(rid, 4040, "team mission not found")
    task_id = str(params.get("task_id") or params.get("taskId") or "").strip()
    approval_nodes: list[dict] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("kind") or "").strip() != "approval_gate":
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        node_task_id = str(
            node.get("task_id")
            or node.get("taskId")
            or metadata.get("task_id")
            or metadata.get("taskId")
            or ""
        ).strip()
        if task_id and node_task_id and node_task_id != task_id:
            continue
        approval_nodes.append(node)
    waiting_nodes = [
        node
        for node in approval_nodes
        if str(node.get("status") or "").strip() == "waiting_approval"
    ]
    if not waiting_nodes:
        mission_status = str(mission.get("status") or "").strip()
        completed_nodes = [
            node
            for node in approval_nodes
            if str(node.get("status") or "").strip() in {"completed", "verified"}
        ]
        if completed_nodes and mission_status != "waiting_approval":
            return _ok(
                rid,
                {
                    "mission_id": mission_id,
                    "already_approved": True,
                    "node": sorted(
                        completed_nodes,
                        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
                    )[-1],
                    "scheduled": {},
                    "graph": graph,
                },
            )
        return _err(rid, 4040, "team mission approval gate not found")
    approval_node = sorted(
        waiting_nodes,
        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
    )[-1]
    node_id = str(approval_node.get("node_id") or approval_node.get("id") or "").strip()
    if not node_id:
        return _err(rid, 4040, "team mission approval gate not found")
    metadata = dict(approval_node.get("metadata") or {})
    metadata.update({
        "approved_by": str(params.get("approved_by") or params.get("approvedBy") or "user").strip() or "user",
    })
    approved_node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(approval_node.get("kind") or "approval_gate"),
        title=str(approval_node.get("title") or "审批任务图"),
        objective=str(approval_node.get("objective") or ""),
        status="completed",
        assignee_profile_id=str(approval_node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(approval_node.get("assignee_profile_version_id") or ""),
        runtime_scope_key=str(approval_node.get("runtime_scope_key") or ""),
        output_contract=dict(approval_node.get("output_contract") or {}),
        metadata=metadata,
        position_x=float(approval_node.get("position_x") or 0),
        position_y=float(approval_node.get("position_y") or 0),
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.plan.approved", "payload": {"node": approved_node}},
        )
    schedule_result = _schedule_ready_nodes(
        db=db,
        rid=rid,
        params={"mission_id": mission_id, "task_id": task_id, "limit": params.get("limit")},
        trigger="team_mission.plan.approve",
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "node": approved_node,
            "scheduled": schedule_result,
            "graph": (schedule_result.get("graph") if isinstance(schedule_result, dict) else None)
            or db.get_team_mission_graph(mission_id),
        },
    )


@method("team_mission.plan.reject")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    result = db.reject_team_mission_plan(
        mission_id=mission_id,
        task_id=str(params.get("task_id") or params.get("taskId") or ""),
        rejected_by=str(params.get("rejected_by") or params.get("rejectedBy") or "user"),
        reason=str(params.get("reason") or ""),
        run_id=_run_id_from_params(params),
    )
    if not result:
        return _err(rid, 4040, "team mission not found")
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "task_id": result.get("task_id") or "",
            "canceled_nodes": list(result.get("canceled_nodes") or []),
            "graph": result.get("graph") or {},
        },
    )


def _recall_collect_conv_message_ids(db, *, conversation_session_id: str, turn_id: str) -> list[int]:
    """Find the conv messages that ``session.recall_turn`` is about to
    deactivate for ``turn_id`` — the user message stamped with the turn id,
    plus every subsequent active assistant / mirror row up to (but not
    including) the next user message. We snapshot these BEFORE the recall so
    we can mirror the deactivation into every member-chat view session that
    has materialized rows pointing back at them.
    """
    try:
        msgs = db.get_messages(conversation_session_id) or []
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
            mt_meta = meta.get("team_mission") if isinstance(meta.get("team_mission"), dict) else {}
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


def _recall_mission_id_from_run(db, *, run_id: str, params: dict, mission: dict | None) -> str:
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
        resolved = db.resolve_team_mission_conversation(conversation_id) if conversation_id else {}
        if isinstance(resolved, dict):
            resolved_mission = resolved.get("mission") if isinstance(resolved.get("mission"), dict) else {}
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
            response = _methods["team_mission.cancel"](rid, {
                "mission_id": mission_id,
                "canceled_by": "user",
                "reason": reason,
            })
        except Exception as exc:
            response = {"error": {"code": 5000, "message": str(exc)}}
        if isinstance(response, dict) and not response.get("error"):
            cancelled_mission_ids.append(mission_id)
            result_payload = response.get("result") if isinstance(response.get("result"), dict) else {}
            for run_info in (result_payload.get("canceled_runs") or []):
                if isinstance(run_info, dict):
                    cancelled_run_ids.append(run_info)
            for err in (result_payload.get("cancel_errors") or []):
                if isinstance(err, dict):
                    cancel_errors.append(err)
        elif isinstance(response, dict) and response.get("error"):
            cancel_errors.append({
                "mission_id": mission_id,
                "message": str(response.get("error", {}).get("message") or "team_mission.cancel failed"),
            })
    if not mission_id and optimistic_run_id:
        # A: direct leader/member conversation run. PR-C makes the conversation
        # session the stored session for both surfaces, so no memberchat lookup
        # or fallback target is needed.
        try:
            cancel_resp = _methods["run.cancel"](rid, {
                "run_id": optimistic_run_id,
                "stored_session_id": conversation_session_id,
                "reason": reason,
            })
        except Exception as exc:
            cancel_resp = {"error": {"code": 5000, "message": str(exc)}}
        if isinstance(cancel_resp, dict) and not cancel_resp.get("error"):
            cancelled_run_ids.append({
                "run_id": optimistic_run_id,
                "stored_session_id": conversation_session_id,
                "status": "cancelled",
            })
        else:
            cancel_errors.append({
                "run_id": optimistic_run_id,
                "message": str((cancel_resp or {}).get("error", {}).get("message") or "leader run.cancel failed"),
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
    client_message_id = str(params.get("client_message_id") or params.get("clientMessageId") or "").strip()
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

    # Step 4 — sync member-chat view sessions: any worker that already
    # materialized rows pointing at the recalled conv messages must drop
    # those rows from its hydration view so its NEXT turn doesn't keep
    # seeing retracted speech. P5 removed the deprecated member_chat_runs
    # registry; the authoritative source for member identities is now the
    # conversation participant roster.
    view_retracted_total = 0
    view_retracted_by_session: dict[str, int] = {}
    if affected_source_ids:
        try:
            participants = db.list_conversation_participants(conversation_session_id) or []
        except Exception:
            participants = []
        seen_view_sessions: set[str] = set()
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            if str(participant.get("role") or "").strip() != "member":
                continue
            mc_member_id = str(participant.get("member_id") or "").strip()
            if not mc_member_id or not conversation_id:
                continue
            view_session_id = f"memberchat:{conversation_id}:{mc_member_id}"
            if view_session_id in seen_view_sessions:
                continue
            seen_view_sessions.add(view_session_id)
            try:
                count = db.recall_member_chat_view_messages(
                    member_chat_session_id=view_session_id,
                    source_message_ids=affected_source_ids,
                )
            except Exception:
                count = 0
            if count:
                view_retracted_by_session[view_session_id] = int(count)
                view_retracted_total += int(count)

    return _ok(rid, {
        "status": "recalled",
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "turn_id": turn_id,
        "recalled": {
            "removed_messages": int(recall_result.get("removed_messages") or 0),
            "source_message_ids": affected_source_ids,
            "view_retracted_total": view_retracted_total,
            "view_retracted_by_session": view_retracted_by_session,
            "interrupted": bool(recall_result.get("interrupted")),
            "draft": recall_result.get("draft") or {},
        },
        "cancelled": {
            "mission_ids": cancelled_mission_ids,
            "runs": cancelled_run_ids,
            "errors": cancel_errors,
        },
        "cascade_type": "B" if cancelled_mission_ids else "A",
    })


def _resolve_cancel_mission_id(db, params: dict) -> str:
    def _existing_mission_id(candidate: str) -> str:
        candidate = str(candidate or "").strip()
        if not candidate:
            return ""
        graph = db.get_team_mission_graph(candidate) if hasattr(db, "get_team_mission_graph") else {}
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return ""
        return str(
            mission.get("mission_id")
            or mission.get("missionId")
            or candidate
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
        conversation = projection.get("conversation") if isinstance(projection.get("conversation"), dict) else {}
        projection_mission = projection.get("mission") if isinstance(projection.get("mission"), dict) else {}
        graph = projection.get("graph") if isinstance(projection.get("graph"), dict) else {}
        graph_mission = graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
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
    if not (_mission_id_from_params(params) or _conversation_id_from_params(params, {}) or _conversation_session_id_from_params(params, {})):
        return _err(rid, 4006, "mission_id or conversation_id required")
    mission_id = _resolve_cancel_mission_id(db, params)
    if not mission_id:
        return _err(rid, 4040, "team mission not found")
    reason = str(params.get("reason") or "").strip() or "Team Mission cancelled by user."
    result = db.cancel_team_mission(
        mission_id=mission_id,
        canceled_by=str(params.get("canceled_by") or params.get("canceledBy") or "user"),
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
        stored_session_id = str(binding.get("session_id") or binding.get("stored_session_id") or "").strip()
        cancel_params = {
            "run_id": run_id,
            "stored_session_id": stored_session_id,
            "runtime_session_id": str(binding.get("runtime_session_id") or ""),
            "runtime_scope_key": str(binding.get("runtime_scope_key") or stored_session_id),
            "reason": reason,
        }
        try:
            response = _methods["run.cancel"](rid, cancel_params)
        except Exception as exc:
            cancel_errors.append({"run_id": run_id, "message": str(exc)})
            continue
        if isinstance(response, dict) and response.get("error"):
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            cancel_errors.append({
                "run_id": run_id,
                "message": str(error.get("message") or response.get("error") or "run cancel failed"),
            })
            continue
        response_result = response.get("result") if isinstance(response, dict) and isinstance(response.get("result"), dict) else {}
        canceled_runs.append({
            "run_id": run_id,
            "stored_session_id": stored_session_id,
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
            "graph": db.get_team_mission_graph(mission_id),
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
    stored_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    if not node_id:
        return _err(rid, 4006, "node_id required")
    if not run_id or not stored_session_id:
        return _err(rid, 4006, "run_id and stored_session_id required")
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return _err(rid, 4040, "team mission node not found")
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or node.get("runtime_scope_key")
        or stored_session_id
    ).strip()
    binding = db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=stored_session_id,
        runtime_session_id=str(params.get("runtime_session_id") or params.get("runtimeSessionId") or ""),
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
        metadata={**dict(node.get("metadata") or {}), "stored_session_id": stored_session_id, "run_id": run_id},
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
    node = db.get_team_mission_node(mission_id, node_id)
    if not node:
        return _err(rid, 4040, "team mission node not found")
    graph = db.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    metadata = dict(node.get("metadata") or {})
    mission_metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    conversation_id = _conversation_id_from_params(params, mission_metadata) or str((mission or {}).get("conversation_id") or mission_id)
    conversation_session_id = _conversation_session_id_from_params(params, mission_metadata) or _team_conversation_session_id(mission if isinstance(mission, dict) else {})
    if isinstance(mission, dict) and mission:
        db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            stable_session_id=conversation_session_id,
            mission=mission,
        )
    if isinstance(mission, dict) and _is_root_planning_node(node):
        mission = _activate_mission_task(db, mission, node, source="team_mission.node.start")
        graph = db.get_team_mission_graph(mission_id)
    if (
        isinstance(mission, dict)
        and _is_root_planning_node(node)
        and not _falsey(params.get("record_user_task_message") if "record_user_task_message" in params else params.get("recordUserTaskMessage"))
    ):
        _append_team_user_task_message(
            db,
            mission=mission,
            objective=str(node.get("objective") or node.get("title") or mission.get("objective") or ""),
            node_id=node_id,
            task_id=str(metadata.get("submitted_task_id") or metadata.get("task_id") or ""),
        )
    stored_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or metadata.get("stored_session_id")
        or _default_node_session_id(mission_id, node_id)
    ).strip()
    try:
        profile_params = _node_profile_params(params, mission if isinstance(mission, dict) else {}, node, db=db)
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or profile_params.get("runtime_scope_key")
        or node.get("runtime_scope_key")
        or stored_session_id
    ).strip()
    run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    turn_id = str(params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex).strip()
    if not db.get_session(stored_session_id):
        db.create_session(stored_session_id, source="team_mission", transient=False)
    try:
        workspace_context = resolve_team_mission_workspace_context(
            params,
            mission=mission if isinstance(mission, dict) else {},
            session_id=stored_session_id,
            require=True,
        )
    except ValueError as exc:
        return _err(rid, 4004, str(exc))
    bind_team_mission_session_workspace(
        session_id=stored_session_id,
        context=workspace_context,
        metadata={
            "source": "team_mission.node.start",
            "conversation_id": conversation_id,
            "mission_id": mission_id,
            "node_id": node_id,
            "team_id": str((mission or {}).get("team_id") or ""),
        },
    )
    runtime_session_error = _ensure_team_mission_runtime_session_shell(stored_session_id)
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    leader_control_node = _is_team_leader_control_node(node)
    base_text = _strategy_start_text(params, mission if isinstance(mission, dict) else {}, node)
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
    task_id = str(
        metadata.get("task_id")
        or metadata.get("taskId")
        or metadata.get("submitted_task_id")
        or metadata.get("submittedTaskId")
        or ""
    ).strip()
    mission_metadata = mission.get("metadata") if isinstance(mission, dict) and isinstance(mission.get("metadata"), dict) else {}
    leader_members = (
        _leader_members_from_params(params, mission if isinstance(mission, dict) else {}, db=db)
        if leader_control_node
        else []
    )
    active_task = dict(mission_metadata.get("active_task") or {}) if isinstance(mission_metadata.get("active_task"), dict) else {}
    if not active_task and _is_root_planning_node(node):
        active_task = {
            "task_id": task_id,
            "title": str(node.get("title") or mission.get("title") or "").strip(),
            "objective": str(node.get("objective") or mission.get("objective") or "").strip(),
            "root_node_id": node_id,
        }
    binding_metadata = {"turn_id": turn_id, "source": "team_mission.node.start"}
    if task_id:
        binding_metadata["task_id"] = task_id
    db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=run_id,
        session_id=stored_session_id,
        runtime_session_id="",
        runtime_scope_key=runtime_scope_key,
        role=_node_role(node),
        metadata={**binding_metadata, "prebound": True},
    )
    submit_params = {
        **params,
        **profile_params,
        "stored_session_id": stored_session_id,
        "session_id": stored_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        "agent_profile_id": agent_profile_id,
        "agent_profile_version_id": agent_profile_version_id,
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        "text": text,
        "enabled_toolsets": enabled_toolsets,
        **({"disabled_toolsets": _leader_disabled_toolsets(params)} if leader_control_node else {}),
        **({"toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE} if leader_control_node or enabled_toolsets else {}),
        "dovie_product_context": {
            **(params.get("dovie_product_context") if isinstance(params.get("dovie_product_context"), dict) else {}),
            "team_mission": {
                "kind": "leader_planning_node" if leader_control_node else "mission_node",
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
                "task_brief": worker_context.get("task_brief") if worker_context else _node_task_brief(node),
                "output_contract": node.get("output_contract") or {},
                "memory": memory_context,
                **({"members": leader_members} if leader_members else {}),
                "worker_context": {
                    key: value
                    for key, value in (worker_context or {}).items()
                    if key not in {"text", "task_brief", "memory"}
                },
                "delegate_inherits_parent_tools": not leader_control_node,
                **({"tool_policy": _team_leader_tool_policy(surface="leader_node")} if leader_control_node else {}),
            },
        },
    }
    response = _submit_run_via_worker_with_response(rid, submit_params)
    if isinstance(response, dict) and response.get("error"):
        response_error = response.get("error") if isinstance(response.get("error"), dict) else {"error": response.get("error")}
        failure = classify_team_mission_failure("error", response_error)
        node = db.upsert_team_mission_node(
            mission_id=mission_id,
            node_id=node_id,
            kind=str(node.get("kind") or "worker"),
            title=str(node.get("title") or ""),
            objective=str(node.get("objective") or ""),
            status="blocked",
            assignee_profile_id=str(node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
            runtime_scope_key=runtime_scope_key,
            output_contract=dict(node.get("output_contract") or {}),
            metadata={
                **metadata,
                "stored_session_id": stored_session_id,
                "start_error": str(response_error.get("message") or response_error.get("error") or ""),
                "effective_toolsets": enabled_toolsets,
                **({
                    "start_error_reason_code": failure["reason_code"],
                    "start_error_recoverability": failure["recoverability"],
                } if failure else {}),
            },
            position_x=float(node.get("position_x") or 0),
            position_y=float(node.get("position_y") or 0),
        )
        return response
    result = response.get("result") if isinstance(response, dict) else {}
    result_run_id = str((result or {}).get("run_id") or run_id).strip()
    result_turn_id = str((result or {}).get("turn_id") or turn_id).strip()
    result_scope = str((result or {}).get("runtime_scope_key") or runtime_scope_key).strip()
    binding_metadata["turn_id"] = result_turn_id
    binding = db.bind_team_mission_run(
        mission_id=mission_id,
        node_id=node_id,
        run_id=result_run_id,
        session_id=stored_session_id,
        runtime_session_id=str((result or {}).get("session_id") or ""),
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
            "stored_session_id": stored_session_id,
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
            "stored_session_id": stored_session_id,
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
