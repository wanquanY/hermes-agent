from __future__ import annotations

import asyncio
import importlib
import time
import uuid
from typing import Any

from hermes_agent.composition.async_sqlite import run_sqlite_io
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.run_worker import RunStartFrame, dovie_product_context_from_params

_server = bind_server_globals(globals())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _async_handle(*, status: str, **identifiers: Any) -> dict[str, Any]:
    return {
        **identifiers,
        "execution_mode": "async",
        "status": status,
        "persistent": True,
    }


def _files(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _prompt_summary(params: dict[str, Any]) -> str:
    summary = _text(params.get("summary") or params.get("prompt_summary") or params.get("promptSummary"))
    if summary:
        return summary[:200]
    return _text(params.get("prompt"))[:200]


def _team_prompt_summary(params: dict[str, Any]) -> str:
    summary = _text(params.get("summary") or params.get("prompt_summary") or params.get("promptSummary"))
    if summary:
        return summary[:200]
    return _text(
        params.get("mission_objective")
        or params.get("missionObjective")
        or params.get("objective")
    )[:200]


def _create_dispatch_activity(
    db: Any,
    *,
    activity_id: str,
    conversation_id: str,
    kind: str,
    parent_activity_id: str = "",
    target_profile_id: str = "",
    target_team_id: str = "",
    target_mission_id: str = "",
    prompt_summary: str = "",
) -> dict[str, Any]:
    return db.activities.create(
        activity_id=activity_id,
        conversation_id=conversation_id,
        kind=kind,
        parent_activity_id=parent_activity_id or None,
        target_profile_id=target_profile_id or None,
        target_team_id=target_team_id or None,
        target_mission_id=target_mission_id or None,
        prompt_summary=prompt_summary,
    )


def _profile_context(profile: dict[str, Any], *, conversation_id: str) -> dict[str, Any]:
    profile_id = _text(profile.get("id") or profile.get("agent_profile_id") or profile.get("agentProfileId"))
    version_id = _text(
        profile.get("agent_profile_version_id")
        or profile.get("agentProfileVersionId")
        or profile.get("current_version_id")
        or profile.get("currentVersionId")
    )
    hermes_home = _text(
        profile.get("hermes_home")
        or profile.get("hermes_home_path")
        or profile.get("hermesHomePath")
        or profile.get("runtime_home_path")
        or profile.get("runtimeHomePath")
    )
    explicit_scope_key = _text(
        profile.get("runtime_scope_key")
        or profile.get("runtimeScopeKey")
    )
    if explicit_scope_key.startswith(("profile:", "team:", "draft:")):
        runtime_scope_key = explicit_scope_key
    elif profile_id:
        runtime_scope_key = f"profile:{profile_id}"
    else:
        runtime_scope_key = explicit_scope_key
    context = dict(profile)
    context.update(
        {
            "id": profile_id,
            "agent_profile_id": profile_id,
            "agentProfileId": profile_id,
            "agent_profile_version_id": version_id,
            "agentProfileVersionId": version_id,
            "hermes_home": hermes_home,
            "hermesHomePath": hermes_home,
            "runtime_scope_key": runtime_scope_key,
            "runtimeScopeKey": runtime_scope_key,
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
        }
    )
    return context


def _workspace_payload_from_parent(params: dict[str, Any], parent_conversation_id: str) -> dict[str, Any]:
    workspace = params.get("workspace") if isinstance(params.get("workspace"), dict) else {}
    cwd = _text(params.get("cwd"))
    workspace_id = _text(params.get("workspace_id") or params.get("workspaceId"))
    workspace_path = _text(params.get("workspace_path") or params.get("workspacePath"))
    if workspace or cwd or workspace_id or workspace_path:
        payload: dict[str, Any] = {}
        if workspace:
            payload["workspace"] = workspace
        if cwd:
            payload["cwd"] = cwd
        if workspace_id:
            payload["workspace_id"] = workspace_id
        if workspace_path:
            payload["workspace_path"] = workspace_path
        return payload
    try:
        from tui_gateway.services.workspace import session_workspace_binding

        binding = session_workspace_binding(parent_conversation_id)
    except Exception:
        binding = None
    if not isinstance(binding, dict) or not binding:
        return {}
    bound_workspace = binding.get("workspace") if isinstance(binding.get("workspace"), dict) else {}
    workspace_path = _text(binding.get("workspace_path") or binding.get("workspacePath") or binding.get("cwd"))
    if not bound_workspace and not workspace_path:
        return {}
    return {
        "workspace": bound_workspace
        or {
            "id": _text(binding.get("workspace_id") or binding.get("workspaceId")),
            "path": workspace_path,
            "name": "workspace",
            "kind": "local",
        },
        "cwd": _text(binding.get("cwd") or workspace_path),
    }


def _team_mission_create_method():
    creator = _methods.get("team_mission.create")
    if creator is not None:
        return creator
    importlib.import_module("hermes_team_mission.gateway.conversation_methods")
    return _methods.get("team_mission.create")


def _jsonrpc_error_message(response: Any, *, fallback: str) -> str:
    if not isinstance(response, dict):
        return fallback
    error = response.get("error")
    if isinstance(error, dict):
        return _text(error.get("message")) or fallback
    return fallback


def _team_mission_result(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    result = response.get("result")
    return result if isinstance(result, dict) else {}


async def dispatch_agent_async(
    params: dict[str, Any],
    *,
    db: Any = None,
    pool: Any = None,
    supervisor: Any = None,
    router: Any = None,
    uuid_factory: Any = None,
    time_fn: Any = None,
) -> dict[str, Any]:
    """Create an async agent-dispatch activity and enqueue a worker run."""

    params = params if isinstance(params, dict) else {}
    target_profile_id = _text(params.get("target_profile_id") or params.get("targetProfileId"))
    prompt = str(params.get("prompt") or "")
    parent_conversation_id = _text(
        params.get("parent_conversation_id")
        or params.get("parentConversationId")
        or params.get("conversation_id")
        or params.get("conversationId")
    )
    parent_activity_id = _text(params.get("parent_activity_id") or params.get("parentActivityId"))
    parent_scope_key = _text(params.get("_parent_scope_key") or params.get("parent_scope_key"))
    parent_hermes_home = _text(params.get("_parent_hermes_home") or params.get("parent_hermes_home"))

    if not parent_conversation_id:
        raise ValueError("parent_conversation_id required")
    if not target_profile_id:
        raise ValueError("target_profile_id required")
    if not prompt.strip():
        raise ValueError("prompt required")

    uuid_factory = uuid_factory or (lambda: uuid.uuid4().hex)
    time_fn = time_fn or time.time
    activity_id = _text(params.get("activity_id") or params.get("activityId")) or str(uuid_factory())
    conversation_id = _text(params.get("conversation_id_new") or params.get("newConversationId")) or str(uuid_factory())
    run_id = _text(params.get("run_id") or params.get("runId")) or str(uuid_factory())
    turn_id = _text(params.get("turn_id") or params.get("turnId")) or str(uuid_factory())
    files = _files(params.get("files"))

    if db is None:
        db = await run_sqlite_io(_get_db)
    if db is None:
        raise RuntimeError("Hermes state db unavailable")

    await run_sqlite_io(
        _create_dispatch_activity,
        db,
        activity_id=activity_id,
        conversation_id=parent_conversation_id,
        kind="agent_dispatch",
        parent_activity_id=parent_activity_id,
        target_profile_id=target_profile_id,
        prompt_summary=_prompt_summary(params),
    )

    try:
        profile = await run_sqlite_io(db.profiles.get_agent_profile, target_profile_id) or {}
    except Exception:
        profile = {}
    if not profile:
        await run_sqlite_io(
            db.activities.update_status,
            activity_id,
            "failed",
            result_summary="profile not found",
            completed_at=time_fn(),
        )
        return _async_handle(
            status="failed",
            activity_id=activity_id,
            conversation_id=conversation_id,
            error="profile not found",
        )

    target_profile_context = _profile_context(profile, conversation_id=conversation_id)

    if pool is None:
        from hermes_agent.orchestration.worker_runtime import worker_pool

        pool = worker_pool()
    if supervisor is None:
        from hermes_agent.orchestration.worker_runtime import worker_supervisor

        supervisor = worker_supervisor()
    if router is None:
        try:
            from hermes_agent.orchestration.worker_runtime import worker_frame_router

            router = worker_frame_router()
        except Exception:
            router = None

    try:
        lease = await pool.get_or_spawn(conversation_id, target_profile_context)
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        await run_sqlite_io(
            db.activities.update_status,
            activity_id,
            "failed",
            result_summary=message,
            completed_at=time_fn(),
        )
        return _async_handle(
            status="failed",
            activity_id=activity_id,
            conversation_id=conversation_id,
            error=message,
        )

    started_at = time_fn()
    await run_sqlite_io(
        db.activities.update_status,
        activity_id,
        "running",
        started_at=started_at,
    )

    if router is not None and callable(getattr(router, "record_run_start", None)):
        router.record_run_start(
            scope_key=lease.scope_key,
            conversation_id=conversation_id,
            run_id=run_id,
            conversation_session_id=conversation_id,
            turn_id=turn_id,
            dispatch_activity_id=activity_id,
            activity_kind="agent_dispatch",
            parent_scope_key=parent_scope_key,
            parent_conversation_id=parent_conversation_id,
            parent_hermes_home=parent_hermes_home,
        )
    if callable(getattr(pool, "record_run_start", None)):
        await pool.record_run_start(
            conversation_id=conversation_id,
            run_id=run_id,
            conversation_session_id=conversation_id,
            turn_id=turn_id,
        )

    frame_params = {
        "dovie_profile": target_profile_context,
        "agent_profile_id": target_profile_context["agent_profile_id"],
        "agentProfileId": target_profile_context["agent_profile_id"],
        "agent_profile_version_id": target_profile_context["agent_profile_version_id"],
        "runtime_scope_key": lease.scope_key,
        "conversation_id": conversation_id,
        "parent_conversation_id": parent_conversation_id,
        "parent_activity_id": parent_activity_id,
        "dispatch_activity_id": activity_id,
        "files": files,
        "source": "agent_dispatch",
    }
    dovie_product_context = dovie_product_context_from_params(params)
    if dovie_product_context:
        frame_params["dovie_product_context"] = dovie_product_context
    send_error = ""
    try:
        ok = await supervisor.send(
            lease.scope_key,
            conversation_id,
            RunStartFrame(
                run_id=run_id,
                turn_id=turn_id,
                conversation_session_id=conversation_id,
                prompt=prompt,
                params=frame_params,
                dovie_product_context=dovie_product_context,
            ),
        )
    except Exception as exc:
        ok = False
        send_error = str(exc) or type(exc).__name__
    finally:
        if callable(getattr(pool, "release", None)):
            await pool.release(conversation_id)
    if not ok:
        if router is not None and callable(getattr(router, "forget_run", None)):
            router.forget_run(run_id)
        if callable(getattr(pool, "forget_run", None)):
            await pool.forget_run(run_id)
        message = send_error or "worker stdin write failed"
        await run_sqlite_io(
            db.activities.update_status,
            activity_id,
            "failed",
            result_summary=message,
            completed_at=time_fn(),
        )
        return _async_handle(
            status="failed",
            activity_id=activity_id,
            conversation_id=conversation_id,
            error=message,
        )

    return _async_handle(
        status="running",
        activity_id=activity_id,
        conversation_id=conversation_id,
    )


async def dispatch_team_async(
    params: dict[str, Any],
    *,
    db: Any = None,
    team_mission_create: Any = None,
    uuid_factory: Any = None,
    time_fn: Any = None,
) -> dict[str, Any]:
    """Create an async team-dispatch activity and start a team mission."""

    params = params if isinstance(params, dict) else {}
    target_team_id = _text(
        params.get("target_team_id")
        or params.get("targetTeamId")
        or params.get("team_id")
        or params.get("teamId")
    )
    mission_objective = str(
        params.get("mission_objective")
        or params.get("missionObjective")
        or params.get("objective")
        or ""
    )
    parent_conversation_id = _text(
        params.get("parent_conversation_id")
        or params.get("parentConversationId")
        or params.get("conversation_id")
        or params.get("conversationId")
    )
    parent_activity_id = _text(params.get("parent_activity_id") or params.get("parentActivityId"))
    parent_scope_key = _text(params.get("_parent_scope_key") or params.get("parent_scope_key"))
    parent_hermes_home = _text(params.get("_parent_hermes_home") or params.get("parent_hermes_home"))

    if not parent_conversation_id:
        raise ValueError("parent_conversation_id required")

    uuid_factory = uuid_factory or (lambda: uuid.uuid4().hex)
    time_fn = time_fn or time.time
    activity_id = _text(params.get("activity_id") or params.get("activityId")) or str(uuid_factory())
    mission_id = _text(params.get("mission_id") or params.get("missionId")) or str(uuid_factory())
    files = _files(params.get("files"))

    if db is None:
        db = await run_sqlite_io(_get_db)
    if db is None:
        raise RuntimeError("Hermes state db unavailable")

    await run_sqlite_io(
        _create_dispatch_activity,
        db,
        activity_id=activity_id,
        conversation_id=parent_conversation_id,
        kind="team_dispatch",
        parent_activity_id=parent_activity_id,
        target_team_id=target_team_id,
        prompt_summary=_team_prompt_summary(params),
    )

    async def _fail(message: str) -> dict[str, Any]:
        await run_sqlite_io(
            db.activities.update_status,
            activity_id,
            "failed",
            result_summary=message,
            completed_at=time_fn(),
        )
        return _async_handle(
            status="failed",
            activity_id=activity_id,
            mission_id=mission_id,
            error=message,
        )

    if not target_team_id:
        return await _fail("target_team_id required")
    if not mission_objective.strip():
        return await _fail("mission_objective required")

    if team_mission_create is None:
        team_mission_create = _team_mission_create_method()
    if not callable(team_mission_create):
        return await _fail("team_mission.create unavailable")

    create_params: dict[str, Any] = {
        "mission_id": mission_id,
        "team_id": target_team_id,
        "title": _text(params.get("title") or params.get("summary")) or mission_objective[:80],
        "objective": mission_objective,
        "metadata": {
            **(params.get("metadata") if isinstance(params.get("metadata"), dict) else {}),
            "start_leader": True,
            "dispatch_activity_id": activity_id,
            "parent_activity_id": parent_activity_id,
            "parent_scope_key": parent_scope_key,
            "parent_hermes_home": parent_hermes_home,
            "parent_conversation_id": parent_conversation_id,
            "files": files,
            "source": "team_dispatch",
        },
    }
    create_params.update(
        await run_sqlite_io(
            _workspace_payload_from_parent,
            params,
            parent_conversation_id,
        )
    )

    try:
        create_response = await run_sqlite_io(
            team_mission_create,
            f"dispatch-team-{activity_id}",
            create_params,
        )
    except Exception as exc:
        return await _fail(str(exc) or type(exc).__name__)

    if isinstance(create_response, dict) and create_response.get("error"):
        return await _fail(
            _jsonrpc_error_message(create_response, fallback="team mission create failed")
        )

    result = _team_mission_result(create_response)
    created_mission_id = _text(result.get("mission_id") or mission_id)
    if not created_mission_id:
        return await _fail("team mission create did not return mission_id")

    await run_sqlite_io(
        db.activities.update_status,
        activity_id,
        "running",
        target_mission_id=created_mission_id,
        started_at=time_fn(),
    )
    return _async_handle(
        status="running",
        activity_id=activity_id,
        mission_id=created_mission_id,
    )


def _run_sync(coro, *, method_name: str):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(f"{method_name} cannot run inside an active event loop")


@method("worker.dispatch_agent_async")
def worker_dispatch_agent_async(rid, params: dict) -> dict:
    """Called by worker subprocess via IPC.

    params: {
      target_profile_id: str,
      prompt: str,
      files: optional[list[str]],
      parent_activity_id: optional[str],
      parent_conversation_id: str,
    }
    Returns: {activity_id, conversation_id}
    """

    try:
        return _ok(
            rid,
            _run_sync(dispatch_agent_async(params), method_name="worker.dispatch_agent_async"),
        )
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"agent dispatch failed: {exc}")


@method("worker.dispatch_team_async")
def worker_dispatch_team_async(rid, params: dict) -> dict:
    """Called by worker subprocess via IPC.

    params: {
      target_team_id: str,
      mission_objective: str,
      files: optional[list[str]],
      parent_activity_id: optional[str],
      parent_conversation_id: str,
    }
    Returns: {activity_id, mission_id}
    """

    try:
        return _ok(
            rid,
            _run_sync(dispatch_team_async(params), method_name="worker.dispatch_team_async"),
        )
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"team dispatch failed: {exc}")
