# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .common import *
from .public_conversation_identity import (
    public_team_conversation,
    public_team_mission_graph,
)

_log = logging.getLogger(__name__)
from .dovie_context import persist_mission_dovie_product_context_from_submit
from .participant_autocreate import (
    resolve_participant_display_identity,
)
from .conversation_owner_entities import (
    ensure_member_chat_owner_entities,
    publish_team_mission_activity_entities,
)
from hermes_profile_dir import resolve_default_agent_dir
from hermes_agent.domain.participants import (
    leader_participant_id,
    member_participant_id,
)
from hermes_team_mission.domain.activity import ACTIVITY_ID_FORMAT_PATTERN
from hermes_team_mission.domain.run_context import RunContext
from hermes_team_mission.runtime.team_transcript_writer import UserSubmissionWriter


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
    return str(
        params.get("target_member_id") or params.get("targetMemberId") or ""
    ).strip()


def _activity_id_from_params(params: dict) -> str:
    activity_id = str(
        params.get("activity_id") or params.get("activityId") or ""
    ).strip()
    if activity_id and not ACTIVITY_ID_FORMAT_PATTERN.match(activity_id):
        raise ValueError("activity_id malformed")
    return activity_id


def _activity_kind_from_activity_id(activity_id: str, *, fallback: str = "chat") -> str:
    normalized = str(activity_id or "").strip()
    if normalized.startswith(("act-team_dispatch-", "act-team_dispatch:")):
        return "team_dispatch"
    if normalized.startswith(("act-member_chat-", "act-member_chat:")):
        return "member_chat"
    if normalized.startswith("mission:") or normalized.startswith("act-node:"):
        return "mission"
    if normalized.startswith("chat:"):
        return "chat"
    return fallback


def _params_request_codex_runtime(params: dict) -> bool:
    if not isinstance(params, dict):
        return False
    runtime_executor = str(
        params.get("runtime_executor") or params.get("runtimeExecutor") or ""
    ).strip()
    return runtime_executor.lower() == "codex"


def _codex_contract_dovie_profile_fields(params: dict) -> dict:
    if not _params_request_codex_runtime(params):
        return {}
    codex_fields = {
        "runtimeExecutor": "codex",
        "runtime_executor": "codex",
    }
    codex_home = str(params.get("codex_home") or params.get("codexHome") or "").strip()
    if codex_home:
        codex_fields["codexHome"] = codex_home
        codex_fields["codex_home"] = codex_home
    codex_account_mode = str(
        params.get("codex_account_mode") or params.get("codexAccountMode") or ""
    ).strip()
    if codex_account_mode:
        codex_fields["codexAccountMode"] = codex_account_mode
        codex_fields["codex_account_mode"] = codex_account_mode
    for raw_extra_env in (params.get("codex_extra_env"), params.get("codexExtraEnv")):
        if not isinstance(raw_extra_env, dict):
            continue
        codex_extra_env = {
            str(key): str(value)
            for key, value in raw_extra_env.items()
            if value is not None
        }
        codex_fields["codexExtraEnv"] = codex_extra_env
        codex_fields["codex_extra_env"] = dict(codex_extra_env)
        break
    return codex_fields


def _ensure_team_dispatch_activity(
    db,
    *,
    activity_id: str,
    conversation_id: str,
    conversation_session_id: str,
    team_id: str = "",
    prompt_summary: str = "",
) -> dict:
    """Ensure the Activity-first request owner exists before Leader execution.

    The row may be bound to a mission later by ``team_mission.create``. Until
    then it is still the stable owner used by Dovie subscriptions.
    """
    normalized_activity_id = str(activity_id or "").strip()
    if not normalized_activity_id:
        return {}
    activities = db.activities
    existing = activities.get(normalized_activity_id)
    if isinstance(existing, dict) and existing:
        return existing
    return activities.create(
        activity_id=normalized_activity_id,
        conversation_id=conversation_session_id or conversation_id,
        kind="team_dispatch",
        target_team_id=team_id or None,
        status="running",
        prompt_summary=prompt_summary[:500] if prompt_summary else None,
        notify_parent=True,
    )


def _find_team_member_by_id(members: list[dict], member_id: str) -> dict:
    member_id = str(member_id or "").strip()
    for member in members or []:
        if not isinstance(member, dict):
            continue
        mid = str(
            member.get("member_id") or member.get("memberId") or member.get("id") or ""
        ).strip()
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
    transcript_activity_kind: str = "",
    draft_text: str = "",
    attachments: list[dict] | None = None,
) -> dict:
    """Persist the user's visible team-conversation turn exactly once."""
    return UserSubmissionWriter.write_user_submission(
        db,
        conversation_id=conversation_id,
        conversation_session_id=conversation_session_id,
        run_id=run_id,
        turn_id=turn_id,
        text=text,
        target_member_id=target_member_id,
        display_name=display_name,
        client_message_id=client_message_id,
        source_kind=source_kind,
        transcript_activity_kind=transcript_activity_kind,
        draft_text=draft_text,
        attachments=attachments,
    )


def _persisted_conversation_message_id(message: dict | None) -> str:
    """Return the stable identity required to bind a pre-persisted input."""
    if not isinstance(message, dict):
        return ""
    return str(
        message.get("conversation_message_id")
        or message.get("conversationMessageId")
        or ""
    ).strip()


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
    proxied = _proxy_run_submit_via_worker(submit_params)
    if proxied.get("error"):
        return _err(rid, 5020, proxied["error"])
    if proxied.get("ok"):
        # primary_dispatch already acknowledged the request on the
        # transport with its own synthetic rid. Construct the
        # JSON-RPC envelope the original caller (with its own rid)
        # expects.
        return _ok(
            rid,
            {
                "status": "queued",
                "run_id": str(
                    submit_params.get("run_id")
                    or submit_params.get("client_run_id")
                    or ""
                ),
                "turn_id": str(submit_params.get("turn_id") or ""),
                "conversation_session_id": str(
                    submit_params.get("conversation_session_id")
                    or submit_params.get("session_id")
                    or ""
                ),
                "runtime_scope_key": str(submit_params.get("runtime_scope_key") or ""),
                "source": "primary-run-worker",
            },
        )
    # Fallback: no transport (background) → in-process. Wrong env
    # but better than dropping the run.
    run_control._diagnostic_warning(  # noqa: SLF001
        "team-mission-run-proxy-fallback-in-process",
        conversation_session_id=str(submit_params.get("conversation_session_id") or ""),
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
    from hermes_agent.orchestration.worker_runtime import (
        ControlPlaneTransport,
        current_worker_runtime_loop,
        primary_dispatch,
    )

    source_transport = current_transport()
    loop = current_worker_runtime_loop()
    if loop is None:
        loop = getattr(source_transport, "_loop", None)
    if loop is None:
        return {"ok": False, "reason": "no_runtime_loop"}
    if not loop.is_running():
        return {"ok": False, "reason": "loop_not_running"}
    try:
        if asyncio.get_running_loop() is loop:
            return {
                "error": "internal run dispatch attempted to synchronously wait on the worker runtime loop"
            }
    except RuntimeError:
        pass
    transport = ControlPlaneTransport(loop=loop)
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


def submit_mission_leader_report_run(**kwargs) -> dict:
    from hermes_team_mission.gateway.leader_report_runtime import (
        submit_mission_leader_report_run as _submit_leader_report_run,
    )

    return _submit_leader_report_run(
        **kwargs,
        run_submitter=_submit_run_via_worker_with_response,
    )


try:
    from hermes_team_mission.runtime.leader_report_dispatch import (
        register_leader_report_submitter,
    )

    register_leader_report_submitter(submit_mission_leader_report_run)
except Exception:
    pass


def _clear_stuck_member_session_run(db, conversation_session_id: str) -> None:
    """Release any non-terminal run left on the member session by a prior turn
    that did not close cleanly (no reaper guards a member-chat session). Keeps
    multi-turn from hitting 'session busy'."""
    import time as _time

    terminal = {
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "canceled",
        "interrupted",
    }
    try:
        with db._lock:
            rows = db._conn.execute(
                "SELECT run_id, status, runtime_scope_key, turn_id FROM runs WHERE session_id = ? ORDER BY rowid DESC LIMIT 5",
                (conversation_session_id,),
            ).fetchall()
    except Exception:
        return
    for row in rows or []:
        run_id = str((row["run_id"] if hasattr(row, "keys") else row[0]) or "").strip()
        status = str((row["status"] if hasattr(row, "keys") else row[1]) or "").lower()
        if not run_id or status in terminal:
            continue
        try:
            db.runs.upsert(
                run_id=run_id,
                session_id=conversation_session_id,
                runtime_scope_key=str(
                    (row["runtime_scope_key"] if hasattr(row, "keys") else row[2]) or ""
                ),
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
    members = _leader_members_from_params(
        params, mission if isinstance(mission, dict) else {}, db=db
    )
    member = _find_team_member_by_id(members, target_member_id)
    if not member:
        return _err(rid, 4040, "team member not found")
    if str(member.get("role") or "").strip().lower() in {"lead", "leader"}:
        return _err(rid, 4006, "target member must be a worker, not the leader")
    profile_params = _profile_params_from_member(member)
    agent_profile_id = str(profile_params.get("agent_profile_id") or "").strip()
    dovie_profile = (
        profile_params.get("dovie_profile")
        if isinstance(profile_params.get("dovie_profile"), dict)
        else {}
    )
    hermes_home = str(
        dovie_profile.get("hermesHomePath")
        or dovie_profile.get("hermes_home_path")
        or ""
    ).strip()
    if not agent_profile_id or not hermes_home:
        return _err(
            rid, 4006, "target member profile is not runnable (missing profile home)"
        )
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
    # AND snake_case keys because worker scope hydration accepts both client
    # dialects; otherwise the member can inherit the default profile scope
    # (e.g. `profile:<id>`) and spawn against the wrong HERMES_HOME.
    dovie_profile = {
        **dovie_profile,
        "runtimeScopeKey": member_scope,
        "runtime_scope_key": member_scope,
        **_codex_contract_dovie_profile_fields(params),
    }
    member_requests_codex_runtime = _params_request_codex_runtime(params)
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
    # STEP ORDER FIX (2026-06-27): The original code did
    #   1. append_message(conv_session, role=user, ...)   ← FK FAIL: conv session row doesn't exist yet
    #   2. create memberchat worker session
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
        params.get("team_id")
        or params.get("teamId")
        or (mission or {}).get("team_id")
        or (mission or {}).get("teamId")
        or ""
    ).strip()
    try:
        ensured_conversation = db.ensure_team_mission_conversation(
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            mission=mission if isinstance(mission, dict) and mission else {},
            mission_id=str((mission or {}).get("mission_id") or "")
            if isinstance(mission, dict) and mission
            else "",
            team_id=team_id_for_ensure,
            title=conversation_title,
            objective=text,
            workspace_id=workspace_context.get("workspace_id")
            if isinstance(workspace_context, dict)
            else "",
            workspace_path=workspace_context.get("workspace_path")
            if isinstance(workspace_context, dict)
            else "",
            created_by_user_id=str(
                params.get("created_by_user_id") or params.get("createdByUserId") or ""
            ).strip(),
            metadata={"display_title_source": "first_user_message"}
            if conversation_title
            else None,
        )
    except Exception as exc:
        return _err(rid, 5008, f"team conversation session unavailable: {exc}")
    try:
        _ensure_team_conversation_session(db, conversation_session_id)
    except Exception as exc:
        return _err(rid, 5008, f"team conversation session unavailable: {exc}")

    # 3. Materialize the member only after its canonical conversation exists.
    # Participant and Activity are published into the same monotonic journal
    # before any run event is allowed to reference them.
    participant_id = member_participant_id(target_member_id)
    try:
        _participant, member_chat_activity = ensure_member_chat_owner_entities(
            db,
            conversation_session_id=conversation_session_id,
            participant_id=participant_id,
            member_id=target_member_id,
            agent_profile_id=agent_profile_id,
            agent_profile_version_id=str(
                profile_params.get("agent_profile_version_id") or ""
            ),
            runtime_scope_key=member_scope,
            display_name=display_name,
            avatar=display_avatar,
            prompt_summary=text,
        )
    except Exception as exc:
        return _err(
            rid,
            5008,
            f"member-chat owner entities unavailable: {exc}",
        )
    display_name, display_avatar = resolve_participant_display_identity(
        db,
        conversation_session_id=conversation_session_id,
        participant_id=participant_id,
        profile_id=agent_profile_id,
        fallback_name=display_name,
        fallback_avatar=display_avatar,
    )

    # 4. Reserve the run/turn identity before writing the user transcript row.
    # The same identity is sent to the worker, so retries/upserts cannot create
    # duplicate user messages and repeated text in later turns remains distinct.
    optimistic_run_id = str(
        params.get("client_run_id") or params.get("run_id") or ""
    ).strip()
    run_id = optimistic_run_id or uuid.uuid4().hex
    turn_id = str(
        params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex
    ).strip()
    client_message_id = str(
        params.get("client_message_id") or params.get("clientMessageId") or ""
    ).strip()
    draft_text = str(params.get("draft_text") or params.get("draftText") or text)
    submitted_attachments = _submitted_attachments(params)

    # 5. NOW it is safe to record the user's @-message into the shared
    # conversation transcript. The conv session row exists, FK satisfied. Use
    # an idempotent projected row; the worker owns execution, not visible user
    # transcript persistence.
    try:
        current_input_message = _upsert_team_user_submission_message(
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
            transcript_activity_kind="member_direct_chat",
            draft_text=draft_text,
            attachments=submitted_attachments,
        )
    except Exception as exc:
        return _err(rid, 5008, f"team user message persistence failed: {exc}")
    current_input_conversation_message_id = _persisted_conversation_message_id(
        current_input_message
    )
    if not current_input_conversation_message_id:
        return _err(rid, 5008, "persisted team user message has no stable identity")

    member_run_home = _home_from_dovie_profile(dovie_profile)
    control_home = _control_plane_home()
    member_memory_ids, member_memory_text = _actor_conversation_memory_text(
        db,
        conversation_session_id=conversation_session_id,
        actor_participant_id=member_participant_id(target_member_id),
        actor_role="member",
        profile_id=agent_profile_id,
        activity_id=str(member_chat_activity["activity_id"]),
    )
    member_actor_fields = _actor_context_snapshot_fields(
        db,
        conversation_session_id=conversation_session_id,
        participant_id=member_participant_id(target_member_id),
        execution_scope_key=member_scope,
        activity_id=str(member_chat_activity["activity_id"]),
        activity_kind="member_chat",
        profile_id=agent_profile_id,
        profile_version_id=str(profile_params.get("agent_profile_version_id") or ""),
        selected_memory_ids=member_memory_ids,
    )
    run_context = RunContext(
        conversation_session_id=conversation_session_id,
        participant_id=member_participant_id(target_member_id),
        activity_id=str(member_chat_activity["activity_id"]),
        activity_kind="member_chat",
        execution_scope_key=member_scope,
        control_home=control_home,
        execution_home=member_run_home,
        **member_actor_fields,
    )

    runtime_session_error = _ensure_team_mission_runtime_session_shell(
        conversation_session_id
    )
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    if not isinstance(ensured_conversation, dict):
        ensured_conversation = {}

    # 6. The worker now runs on the conversation session itself. With no
    # memberchat mirror registry to rewrite run ids, use the frontend's
    # pre-reserved optimistic run id directly when present so terminal frames
    # settle the same conversation-side run the UI is tracking.

    # 7. run.submit with CLEAN member params only — NOT {**params} (which carries
    #    the frontend's leader scope/profile and breaks the worker spawn).
    submit_params = {
        "conversation_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "current_input_conversation_message_id": current_input_conversation_message_id,
        "agent_profile_id": agent_profile_id,
        "agent_profile_version_id": str(
            profile_params.get("agent_profile_version_id") or ""
        ),
        "runtime_scope_key": member_scope,
        "run_context_json": _run_context_json(run_context),
        "dovie_profile": dovie_profile,
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        # The worker receives the enriched execution text, while the shared
        # transcript row above keeps the user's clean draft text plus attachment
        # metadata for history hydration.
        "text": text,
        "turn_system_context": _member_conversation_context(
            display_name=display_name,
            memory_text=member_memory_text,
        ),
        "user_message_persistence": "external",
        "draft_text": draft_text,
        "attachments": submitted_attachments,
        "tool_progress_mode": "all",
        "cols": 120,
        "dovie_product_context": _team_dovie_product_context(
            params,
            team_mission={
                "kind": "member_chat",
                "surface": "member_chat",
                "conversation_id": conversation_id,
                "conversation_session_id": conversation_session_id,
                "member_id": target_member_id,
            },
            executing_agent_profile_id=agent_profile_id,
            agent_role="team_member",
        ),
    }
    for route_field in (
        "resolution_id",
        "route_fingerprint",
        "execution_target",
        "expected_session_revision",
    ):
        if params.get(route_field) not in (None, "", {}, []):
            submit_params[route_field] = params[route_field]
    if not member_requests_codex_runtime and "model" in params:
        submit_params["model"] = params["model"]
    if not member_requests_codex_runtime and (
        params.get("model_descriptor") or params.get("modelDescriptor")
    ):
        submit_params["model_descriptor"] = (
            params.get("model_descriptor") or params.get("modelDescriptor")
        )
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
            conversation_session_id=conversation_session_id,
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
        "conversation_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "worker_conversation_session_id": conversation_session_id,
        "runtime_scope_key": member_scope,
        "status": "streaming",
    }
    # Use the conversation we just ensured (guaranteed to carry canonical
    # fields). For graph, fall back to the read model — it may be empty for
    # a brand-new conv with no mission, but that's fine: the frontend
    # accepts an empty graph here, only the conversation payload is
    # canonical-checked.
    resolved = (
        db.resolve_team_mission_conversation(conversation_id) if conversation_id else {}
    )
    resolved = resolved if isinstance(resolved, dict) else {}
    return _ok(
        rid,
        {
            "conversation_session_id": conversation_session_id,
            "conversation": public_team_conversation(
                ensured_conversation or resolved.get("conversation") or {}
            ),
            "graph": public_team_mission_graph(resolved.get("graph") or {}),
            "leader_turn": member_turn,
            "member_turn": member_turn,
            "target_member_id": target_member_id,
        },
    )


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
    if not mission_id and not conversation_session_id and not conversation_id:
        return _err(rid, 4006, "mission_id or conversation_session_id required")
    graph = (
        db.team_mission_graphs.get_team_mission_graph(mission_id) if mission_id else {}
    )
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
        resolved_identifier = conversation_session_id or conversation_id
        resolved = (
            db.resolve_team_mission_conversation(resolved_identifier)
            if resolved_identifier
            else {}
        )
        if isinstance(resolved, dict):
            conversation = (
                resolved.get("conversation")
                if isinstance(resolved.get("conversation"), dict)
                else {}
            )
            resolved_graph = (
                resolved.get("graph") if isinstance(resolved.get("graph"), dict) else {}
            )
            resolved_mission = (
                resolved.get("mission")
                if isinstance(resolved.get("mission"), dict)
                else {}
            )
            if resolved_mission:
                context_mission = resolved_mission
                context_graph = resolved_graph
                if explicit_mission_request:
                    mission = resolved_mission
                    graph = resolved_graph
                    mission_id = str(mission.get("mission_id") or "").strip()
    identity_mission = (
        mission if isinstance(mission, dict) and mission else context_mission
    )
    if isinstance(identity_mission, dict) and identity_mission:
        identity_mission = persist_mission_dovie_product_context_from_submit(
            db, identity_mission, params
        )
        if mission and str(mission.get("mission_id") or "") == str(
            identity_mission.get("mission_id") or ""
        ):
            mission = identity_mission
        if context_mission and str(context_mission.get("mission_id") or "") == str(
            identity_mission.get("mission_id") or ""
        ):
            context_mission = identity_mission
    metadata = (
        identity_mission.get("metadata")
        if isinstance(identity_mission, dict)
        and isinstance(identity_mission.get("metadata"), dict)
        else {}
    )
    conversation_id = (
        str((conversation or {}).get("conversation_id") or "").strip()
        or str((mission or {}).get("conversation_id") or "").strip()
        or conversation_id
        or conversation_session_id
        or mission_id
    )
    if not conversation_id:
        return _err(rid, 4006, "conversation_id required")
    conversation_session_id = (
        conversation_session_id
        or str((conversation or {}).get("conversation_session_id") or "").strip()
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
        _team_id_for_profile(
            params,
            mission=identity_mission if isinstance(identity_mission, dict) else {},
            conversation=conversation,
        ),
    )
    if archived_team_error:
        return _err(rid, 4023, archived_team_error)
    try:
        request_activity_id = _activity_id_from_params(params)
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    target_member_id = _target_member_id_from_params(params)
    if request_activity_id and not target_member_id:
        try:
            _ensure_team_dispatch_activity(
                db,
                activity_id=request_activity_id,
                conversation_id=conversation_id,
                conversation_session_id=conversation_session_id,
                team_id=str(
                    params.get("team_id")
                    or params.get("teamId")
                    or (identity_mission or {}).get("team_id")
                    or ""
                ),
                prompt_summary=text,
            )
        except Exception as exc:
            return _err(rid, 5008, f"team dispatch activity create failed: {exc}")
    # ADR-0001 Activity-first: the leader turn belongs to a stable request
    # activity when the frontend provides one. Legacy callers fall back to the
    # chat activity; no task runtime owner is ever `team-conversation:*`.
    # Group-chat: route directly to a worker member, bypassing the leader.
    if target_member_id:
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
        params, leader_runtime_context = (
            _resolve_team_leader_runtime_params_for_request(
                params,
                (graph if isinstance(graph, dict) and graph else context_graph)
                if isinstance(context_graph, dict)
                else {},
                db,
            )
        )
    except ValueError as exc:
        return _err(rid, 4094, str(exc))
    prompt_graph = (
        (graph if isinstance(graph, dict) and graph else context_graph)
        if isinstance(context_graph, dict)
        else {}
    )
    profile_params = _leader_profile_params(
        params, prompt_graph if isinstance(prompt_graph, dict) else {}
    )
    runtime_scope_key = _leader_conversation_runtime_scope_key(
        params,
        conversation_id=conversation_id,
        mission_id=mission_id,
    )
    contract_error = _leader_conversation_runtime_scope_contract_error(
        params, runtime_scope_key
    )
    if contract_error:
        return _err(rid, 4094, contract_error)
    owner_error = _leader_runtime_owner_error(
        profile_params, leader_runtime_scope_key=runtime_scope_key
    )
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
            conversation_session_id=conversation_session_id,
            mission=mission if isinstance(mission, dict) and mission else {},
            mission_id=mission_id if isinstance(mission, dict) and mission else "",
            team_id=str(
                params.get("team_id")
                or params.get("teamId")
                or (identity_mission or {}).get("team_id")
                or ""
            ),
            title=conversation_title,
            objective=str(
                params.get("objective")
                or params.get("prompt")
                or (identity_mission or {}).get("objective")
                or text
            ),
            workspace_id=workspace_context["workspace_id"],
            workspace_path=workspace_context["workspace_path"],
            created_by_user_id=str(
                params.get("created_by_user_id")
                or params.get("createdByUserId")
                or (identity_mission or {}).get("created_by_user_id")
                or ""
            ),
            metadata={"display_title_source": "first_user_message"}
            if conversation_title
            else None,
        )
        bind_team_mission_session_workspace(
            session_id=conversation_session_id,
            context=workspace_context,
            metadata={
                "source": "team_mission.message.submit",
                "conversation_id": conversation_id,
                **({"mission_id": mission_id} if mission_id else {}),
                "team_id": str(
                    params.get("team_id")
                    or params.get("teamId")
                    or (identity_mission or {}).get("team_id")
                    or ""
                ),
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
    memory_context, memory_text = {}, ""
    run_id = str(
        params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex
    ).strip()
    turn_id = str(
        params.get("turn_id") or params.get("turnId") or uuid.uuid4().hex
    ).strip()
    client_message_id = str(
        params.get("client_message_id") or params.get("clientMessageId") or ""
    ).strip()
    draft_text = str(params.get("draft_text") or params.get("draftText") or text)
    submitted_attachments = _submitted_attachments(params)
    direct_reply = _leader_message_requests_direct_reply(text)
    team_context = {
        "kind": "leader_conversation",
        "conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "team_id": str(
            params.get("team_id")
            or params.get("teamId")
            or (identity_mission or {}).get("team_id")
            or (conversation or {}).get("team_id")
            or ""
        ),
        "mode": (mission or {}).get("mode") or str(params.get("mode") or ""),
        "status": (mission or {}).get("status") or "",
        "workspace_id": workspace_context["workspace_id"],
        "workspace_path": workspace_context["workspace_path"],
        "memory": memory_context,
        "members": _leader_members_from_params(
            params, mission if isinstance(mission, dict) else {}
        ),
        "tool_policy": _team_leader_tool_policy(surface="leader_conversation"),
    }
    snapshot_id = _team_capability_snapshot_id(params)
    if snapshot_id:
        team_context["team_capability_snapshot_id"] = snapshot_id
    if mission_id:
        team_context["mission_id"] = mission_id
    if request_activity_id:
        team_context["activity_id"] = request_activity_id
        team_context["activityId"] = request_activity_id
        team_context["request_activity_id"] = request_activity_id
        team_context["requestActivityId"] = request_activity_id
    # A conversation-only leader turn may include a historical mission in
    # prompt context, but that mission must not own the new run. Otherwise a
    # follow-up after cancel/retry is written to the old mission activity and
    # the current team room never receives the leader's start-task event.
    activity_mission_id = (
        str(mission_id or "").strip() if explicit_mission_request else ""
    )
    if request_activity_id:
        leader_activity_id = request_activity_id
        leader_activity_kind = _activity_kind_from_activity_id(
            request_activity_id,
            fallback="team_dispatch",
        )
    elif activity_mission_id:
        leader_activity_id = f"mission:{activity_mission_id}"
        leader_activity_kind = "mission"
    else:
        leader_activity_id = f"chat:{conversation_session_id}"
        leader_activity_kind = "chat"
    actor_memory_ids, actor_memory_text = _actor_conversation_memory_text(
        db,
        conversation_session_id=conversation_session_id,
        actor_participant_id=leader_participant_id(conversation_id),
        actor_role="leader",
        profile_id=str(profile_params.get("agent_profile_id") or ""),
        activity_id=leader_activity_id,
    )
    effective_memory_text = "\n\n".join(
        part for part in (memory_text, actor_memory_text) if part
    )
    if actor_memory_ids:
        team_context["conversation_memory_item_ids"] = actor_memory_ids
    leader_run_home = _home_from_profile_params(profile_params)
    control_home = _control_plane_home()
    leader_actor_fields = _actor_context_snapshot_fields(
        db,
        conversation_session_id=conversation_session_id,
        participant_id=leader_participant_id(conversation_id),
        execution_scope_key=runtime_scope_key,
        activity_id=leader_activity_id,
        activity_kind=leader_activity_kind,
        profile_id=str(profile_params.get("agent_profile_id") or ""),
        profile_version_id=str(profile_params.get("agent_profile_version_id") or ""),
        selected_memory_ids=[
            *list((memory_context or {}).get("item_ids") or []),
            *actor_memory_ids,
        ],
    )
    run_context = RunContext(
        conversation_session_id=conversation_session_id,
        participant_id=leader_participant_id(conversation_id),
        activity_id=leader_activity_id,
        activity_kind=leader_activity_kind,
        execution_scope_key=runtime_scope_key,
        control_home=control_home,
        execution_home=leader_run_home,
        **leader_actor_fields,
    )
    try:
        current_input_message = _upsert_team_user_submission_message(
            db,
            conversation_id=conversation_id,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            turn_id=turn_id,
            text=draft_text,
            client_message_id=client_message_id,
            source_kind="leader_chat_user",
            transcript_activity_kind=(
                "mission_start"
                if activity_mission_id or leader_activity_kind == "team_dispatch"
                else "leader_chat"
            ),
            draft_text=draft_text,
            attachments=submitted_attachments,
        )
    except Exception as exc:
        return _err(rid, 5008, f"team user message persistence failed: {exc}")
    current_input_conversation_message_id = _persisted_conversation_message_id(
        current_input_message
    )
    if not current_input_conversation_message_id:
        return _err(rid, 5008, "persisted team user message has no stable identity")
    submit_params = {
        **params,
        **profile_params,
        "conversation_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "client_run_id": run_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "current_input_conversation_message_id": current_input_conversation_message_id,
        "runtime_scope_key": runtime_scope_key,
        "run_context_json": _run_context_json(run_context),
        "agent_context_mode": "team_leader",
        "cwd": workspace_context["cwd"],
        "workspace": workspace_context["workspace"],
        # The provider user message is exactly the user's input. Team identity,
        # routing policy, graph state, and memory are trusted per-turn system
        # context and must never be flattened into user-role content.
        "text": text,
        "turn_system_context": (
            _leader_direct_reply_context(
                graph=prompt_graph
                if isinstance(prompt_graph, dict) and prompt_graph
                else graph,
                memory_text=effective_memory_text,
            )
            if direct_reply
            else _leader_router_context(
                graph=prompt_graph
                if isinstance(prompt_graph, dict) and prompt_graph
                else graph,
                memory_text=effective_memory_text,
            )
        ),
        # UserSubmissionWriter above owns the canonical visible user row.
        # The runtime keeps the pure user input for inference but must not
        # append a second transcript row for the same run/turn.
        "user_message_persistence": "external",
        "draft_text": draft_text,
        "attachments": submitted_attachments,
        "enabled_toolsets": [] if direct_reply else _leader_message_toolsets(params),
        "disabled_toolsets": _leader_disabled_toolsets(params),
        "toolset_scope": _TEAM_LEADER_TOOLSET_SCOPE,
        "dovie_product_context": _team_dovie_product_context(
            params,
            team_mission=team_context,
            executing_agent_profile_id=str(
                profile_params.get("agent_profile_id")
                or params.get("agent_profile_id")
                or params.get("agentProfileId")
                or ""
            ),
            agent_role="team_leader",
        ),
    }
    if direct_reply:
        submit_params["reasoning_config"] = dict(
            _TEAM_LEADER_DIRECT_REPLY_REASONING_CONFIG
        )
    runtime_session_error = _ensure_team_mission_runtime_session_shell(
        conversation_session_id
    )
    if runtime_session_error:
        return _err(rid, 5008, runtime_session_error)
    response = _submit_run_via_worker_with_response(rid, submit_params)
    if isinstance(response, dict) and response.get("error"):
        return response
    result = response.get("result") if isinstance(response, dict) else {}
    if isinstance(mission, dict) and mission and submitted_attachments:
        _record_leader_input_attachment_artifacts(
            db,
            mission=mission,
            conversation_session_id=conversation_session_id,
            run_id=run_id,
            attachments=submitted_attachments,
        )
    ensure_team_leader_message_run_state(
        db,
        run_id=run_id,
        session_id=conversation_session_id,
        runtime_scope_key=runtime_scope_key,
        result=result,
    )
    leader_turn = _leader_turn_for_this_submit(
        result,
        run_id=run_id,
        turn_id=turn_id,
        conversation_session_id=conversation_session_id,
        runtime_scope_key=runtime_scope_key,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "activity_id": request_activity_id or leader_activity_id,
            "conversation_session_id": conversation_session_id,
            "conversation": public_team_conversation(conversation),
            "leader_turn": leader_turn,
            "leader_runtime_context": leader_runtime_context,
            "leaderRuntimeContext": leader_runtime_context,
            "graph": public_team_mission_graph(
                db.team_mission_graphs.get_team_mission_graph(mission_id)
                if mission_id
                else graph
            ),
        },
    )


def _leader_turn_for_this_submit(
    result: dict | None,
    *,
    run_id: str,
    turn_id: str,
    conversation_session_id: str,
    runtime_scope_key: str,
) -> dict:
    """Build the leader_turn descriptor for THIS submission.

    run.submit may answer with the session's currently-streaming turn (e.g. a
    still-open member-chat turn when the leader prompt gets queued behind it).
    The submit reply contract is per-submission identity: the client keys its
    optimistic run reconciliation on these fields, so echoing another turn's
    run_id/turn_id makes the desktop settle the fresh leader run against an
    already-terminal run (instant-complete regression, 2026-07-02 real-device
    log). Force the identity fields; keep worker-result extras only when they
    describe this run.
    """
    worker_result = dict(result) if isinstance(result, dict) else {}
    worker_run_id = str(worker_result.get("run_id") or "").strip()
    if worker_run_id and worker_run_id != run_id:
        # Foreign turn descriptor — its session/runtime fields belong to the
        # other run; drop them instead of leaking them onto this submission.
        worker_result = {}
    return {
        **worker_result,
        "run_id": run_id,
        "turn_id": turn_id,
        "conversation_session_id": conversation_session_id,
        "session_id": worker_result.get("session_id") or conversation_session_id,
        "runtime_scope_key": runtime_scope_key,
    }


@method("team_mission.graph")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    requested_conversation_session_id = _conversation_session_id_from_params(params)
    legacy_conversation_id = _conversation_id_from_params(params)
    conversation_identifier = requested_conversation_session_id or legacy_conversation_id
    conversation_session_id = ""
    conversation_id = ""
    if conversation_identifier:
        resolved = db.resolve_team_mission_conversation(conversation_identifier)
        conversation = (
            resolved.get("conversation")
            if isinstance(resolved, dict)
            and isinstance(resolved.get("conversation"), dict)
            else {}
        )
        conversation_id = str(conversation.get("conversation_id") or "").strip()
        conversation_session_id = str(
            conversation.get("conversation_session_id") or ""
        ).strip()
    if conversation_id:
        graph = db.team_mission_graphs.get_team_mission_conversation_graph(
            conversation_id
        )
        if not graph:
            return _err(rid, 4040, "team mission conversation not found")
        mission_ids = _graph_mission_ids(graph)
        if mission_id and mission_id not in mission_ids:
            return _err(rid, 4040, "team mission not found in conversation")
        graph_mission = (
            graph.get("mission") if isinstance(graph.get("mission"), dict) else {}
        )
        graph_mission_id = str(
            graph_mission.get("mission_id") or graph_mission.get("missionId") or ""
        ).strip()
        result = {
            "mission_id": graph_mission_id or mission_id,
            **(
                {"conversation_session_id": conversation_session_id}
                if conversation_session_id
                else {}
            ),
            "graph": public_team_mission_graph(graph),
        }
        if mission_id and mission_id != result["mission_id"]:
            result["requested_mission_id"] = mission_id
        return _ok(rid, result)
    if not mission_id:
        return _err(rid, 4006, "mission_id required")
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    if not graph:
        return _err(rid, 4040, "team mission not found")
    return _ok(rid, {"mission_id": mission_id, "graph": public_team_mission_graph(graph)})


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
    byte_limit = _bounded_byte_limit(
        params.get("byte_limit") or params.get("byteLimit")
    )
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
            "last_event_seq": max(
                [int(event.get("seq") or 0) for event in events], default=after_seq
            ),
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
    node_id = str(
        payload.get("node_id") or payload.get("nodeId") or payload.get("id") or ""
    ).strip()
    if not node_id:
        return _err(rid, 4006, "node_id required")
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    if not isinstance(mission, dict) or not mission:
        return _err(rid, 4040, "team mission not found")
    metadata = payload.get("metadata") or {}
    output_contract = (
        payload.get("output_contract") or payload.get("outputContract") or {}
    )
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "node.metadata must be an object")
    if not isinstance(output_contract, dict):
        return _err(rid, 4004, "node.output_contract must be an object")
    metadata = dict(metadata)
    assignee_member_id = str(
        payload.get("assignee_member_id") or payload.get("assigneeMemberId") or ""
    ).strip()
    assignee_role = str(
        payload.get("assignee_role") or payload.get("assigneeRole") or ""
    ).strip()
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
        assignee_profile_id=str(
            payload.get("assignee_profile_id") or payload.get("assigneeProfileId") or ""
        ),
        assignee_profile_version_id=str(
            payload.get("assignee_profile_version_id")
            or payload.get("assigneeProfileVersionId")
            or ""
        ),
        runtime_scope_key=str(
            payload.get("runtime_scope_key") or payload.get("runtimeScopeKey") or ""
        ),
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
    else:
        publish_team_mission_activity_entities(db, mission_id=mission_id)
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else {}
    node_metadata = (
        node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    )
    if isinstance(mission, dict) and _is_root_planning_node(node):
        mission = _activate_mission_task(
            db, mission, node, source="team_mission.node.create"
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
            objective=str(node.get("objective") or node.get("title") or ""),
            node_id=str(node.get("node_id") or ""),
            task_id=str(
                node_metadata.get("submitted_task_id")
                or node_metadata.get("task_id")
                or ""
            ),
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
    from_node_id = str(
        payload.get("from_node_id")
        or payload.get("fromNodeId")
        or payload.get("source")
        or ""
    ).strip()
    to_node_id = str(
        payload.get("to_node_id")
        or payload.get("toNodeId")
        or payload.get("target")
        or ""
    ).strip()
    if not from_node_id or not to_node_id:
        return _err(rid, 4006, "from_node_id and to_node_id required")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        return _err(rid, 4004, "edge.metadata must be an object")
    edge = db.upsert_team_mission_edge(
        mission_id=mission_id,
        edge_id=str(
            payload.get("edge_id") or payload.get("edgeId") or payload.get("id") or ""
        ),
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
    else:
        publish_team_mission_activity_entities(db, mission_id=mission_id)
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "edge": edge,
            "graph": db.team_mission_graphs.get_team_mission_graph(mission_id),
        },
    )


@method("team_mission.node.update")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    payload = _node_payload_from_params(params)
    node_id = str(
        payload.get("node_id") or payload.get("nodeId") or payload.get("id") or ""
    ).strip()
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
    assignee_member_id = str(
        payload.get("assignee_member_id") or payload.get("assigneeMemberId") or ""
    ).strip()
    assignee_role = str(
        payload.get("assignee_role") or payload.get("assigneeRole") or ""
    ).strip()
    if assignee_member_id:
        metadata.setdefault("assignee_member_id", assignee_member_id)
    if assignee_role:
        metadata.setdefault("assignee_role", assignee_role)
    output_contract = dict(existing.get("output_contract") or {})
    if isinstance(
        payload.get("output_contract") or payload.get("outputContract"), dict
    ):
        output_contract.update(
            payload.get("output_contract") or payload.get("outputContract") or {}
        )
    node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(payload.get("kind") or existing.get("kind") or "worker"),
        title=str(payload.get("title") or existing.get("title") or ""),
        objective=str(payload.get("objective") or existing.get("objective") or ""),
        status=str(payload.get("status") or existing.get("status") or "todo"),
        assignee_profile_id=str(
            payload.get("assignee_profile_id")
            or payload.get("assigneeProfileId")
            or existing.get("assignee_profile_id")
            or ""
        ),
        assignee_profile_version_id=str(
            payload.get("assignee_profile_version_id")
            or payload.get("assigneeProfileVersionId")
            or existing.get("assignee_profile_version_id")
            or ""
        ),
        runtime_scope_key=str(
            payload.get("runtime_scope_key")
            or payload.get("runtimeScopeKey")
            or existing.get("runtime_scope_key")
            or ""
        ),
        output_contract=output_contract,
        metadata=metadata,
        position_x=float(
            payload.get("position_x")
            or payload.get("x")
            or existing.get("position_x")
            or 0
        ),
        position_y=float(
            payload.get("position_y")
            or payload.get("y")
            or existing.get("position_y")
            or 0
        ),
    )
    run_id = _run_id_from_params(params)
    if run_id:
        db.append_team_mission_run_event(
            mission_id=mission_id,
            run_id=run_id,
            event={"type": "mission.node.updated", "payload": {"node": node}},
        )
    else:
        publish_team_mission_activity_entities(db, mission_id=mission_id)
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
            "graph": (
                schedule_result.get("graph")
                if isinstance(schedule_result, dict)
                else None
            )
            or db.team_mission_graphs.get_team_mission_graph(mission_id),
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
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
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
            "graph": (
                schedule_result.get("graph")
                if isinstance(schedule_result, dict)
                else None
            )
            or result.get("graph")
            or {},
        },
    )


@method("team_mission.plan.approve")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        _log.info(
            "[team_mission.plan.approve] rejected: db unavailable rid=%s",
            rid,
        )
        return _db_unavailable_error(rid, code=5008)
    mission_id = _mission_id_from_params(params)
    if not mission_id:
        _log.info(
            "[team_mission.plan.approve] rejected: mission_id missing rid=%s",
            rid,
        )
        return _err(rid, 4006, "mission_id required")
    task_id_log = str(params.get("task_id") or params.get("taskId") or "")
    approved_by_log = str(
        params.get("approved_by") or params.get("approvedBy") or "user"
    )
    run_id_log = _run_id_from_params(params)
    _log.info(
        "[team_mission.plan.approve] enter mission_id=%s task_id=%s approved_by=%s run_id=%s rid=%s",
        mission_id,
        task_id_log,
        approved_by_log,
        run_id_log,
        rid,
    )
    graph = db.team_mission_graphs.get_team_mission_graph(mission_id)
    mission = graph.get("mission") if isinstance(graph, dict) else None
    if not isinstance(mission, dict):
        _log.warning(
            "[team_mission.plan.approve] mission not found mission_id=%s rid=%s",
            mission_id,
            rid,
        )
        return _err(rid, 4040, "team mission not found")
    task_id = str(params.get("task_id") or params.get("taskId") or "").strip()
    approval_nodes: list[dict] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("kind") or "").strip() != "approval_gate":
            continue
        metadata = (
            node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        )
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
            _log.info(
                "[team_mission.plan.approve] already approved (idempotent) mission_id=%s task_id=%s rid=%s",
                mission_id,
                task_id,
                rid,
            )
            return _ok(
                rid,
                {
                    "mission_id": mission_id,
                    "already_approved": True,
                    "node": sorted(
                        completed_nodes,
                        key=lambda item: float(
                            item.get("updated_at") or item.get("created_at") or 0
                        ),
                    )[-1],
                    "scheduled": {},
                    "graph": graph,
                },
            )
        _log.warning(
            "[team_mission.plan.approve] approval gate not found mission_id=%s task_id=%s mission_status=%s approval_node_count=%d rid=%s",
            mission_id,
            task_id,
            mission_status,
            len(approval_nodes),
            rid,
        )
        return _err(rid, 4040, "team mission approval gate not found")
    approval_node = sorted(
        waiting_nodes,
        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
    )[-1]
    node_id = str(approval_node.get("node_id") or approval_node.get("id") or "").strip()
    if not node_id:
        _log.warning(
            "[team_mission.plan.approve] approval node missing id mission_id=%s task_id=%s rid=%s",
            mission_id,
            task_id,
            rid,
        )
        return _err(rid, 4040, "team mission approval gate not found")
    metadata = dict(approval_node.get("metadata") or {})
    metadata.update({
        "approved_by": str(
            params.get("approved_by") or params.get("approvedBy") or "user"
        ).strip()
        or "user",
    })
    approved_node = db.upsert_team_mission_node(
        mission_id=mission_id,
        node_id=node_id,
        kind=str(approval_node.get("kind") or "approval_gate"),
        title=str(approval_node.get("title") or "审批任务图"),
        objective=str(approval_node.get("objective") or ""),
        status="completed",
        assignee_profile_id=str(approval_node.get("assignee_profile_id") or ""),
        assignee_profile_version_id=str(
            approval_node.get("assignee_profile_version_id") or ""
        ),
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
    else:
        publish_team_mission_activity_entities(db, mission_id=mission_id)
    schedule_result = _schedule_ready_nodes(
        db=db,
        rid=rid,
        params={
            "mission_id": mission_id,
            "task_id": task_id,
            "limit": params.get("limit"),
        },
        trigger="team_mission.plan.approve",
    )
    scheduled_ready_nodes = 0
    if isinstance(schedule_result, dict):
        try:
            scheduled_ready_nodes = int(schedule_result.get("ready_nodes_count") or 0)
        except (TypeError, ValueError):
            scheduled_ready_nodes = 0
    _log.info(
        "[team_mission.plan.approve] ok mission_id=%s task_id=%s node_id=%s scheduled_ready=%d rid=%s",
        mission_id,
        task_id,
        node_id,
        scheduled_ready_nodes,
        rid,
    )
    return _ok(
        rid,
        {
            "mission_id": mission_id,
            "node": approved_node,
            "scheduled": schedule_result,
            "graph": (
                schedule_result.get("graph")
                if isinstance(schedule_result, dict)
                else None
            )
            or db.team_mission_graphs.get_team_mission_graph(mission_id),
        },
    )


# Export underscore-prefixed helpers for the gateway registration surface.
__all__ = [name for name in globals() if not name.startswith("__")]
