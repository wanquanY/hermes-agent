from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.run_worker import RunStartFrame

_server = bind_server_globals(globals())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _files(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _prompt_summary(params: dict[str, Any]) -> str:
    summary = _text(params.get("summary") or params.get("prompt_summary") or params.get("promptSummary"))
    if summary:
        return summary[:200]
    return _text(params.get("prompt"))[:200]


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
            "runtime_scope_key": conversation_id,
            "runtimeScopeKey": conversation_id,
        }
    )
    return context


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
        db = _get_db()
    if db is None:
        raise RuntimeError("Hermes state db unavailable")

    db.create_activity(
        activity_id=activity_id,
        conversation_id=parent_conversation_id,
        kind="agent_dispatch",
        parent_activity_id=parent_activity_id or None,
        target_profile_id=target_profile_id,
        prompt_summary=_prompt_summary(params),
    )

    profile = {}
    try:
        getter = getattr(db, "get_agent_profile", None)
        if callable(getter):
            profile = getter(target_profile_id) or {}
    except Exception:
        profile = {}
    if not profile:
        db.update_activity_status(
            activity_id,
            "failed",
            result_summary="profile not found",
            completed_at=time_fn(),
        )
        return {
            "activity_id": activity_id,
            "conversation_id": conversation_id,
            "status": "failed",
            "error": "profile not found",
        }

    target_profile_context = _profile_context(profile, conversation_id=conversation_id)

    if pool is None:
        from tui_gateway.services.worker_runtime import worker_pool

        pool = worker_pool()
    if supervisor is None:
        from tui_gateway.services.worker_runtime import worker_supervisor

        supervisor = worker_supervisor()
    if router is None:
        try:
            from tui_gateway.services.worker_runtime import worker_frame_router

            router = worker_frame_router()
        except Exception:
            router = None

    try:
        lease = await pool.get_or_spawn(conversation_id, target_profile_context)
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        db.update_activity_status(
            activity_id,
            "failed",
            result_summary=message,
            completed_at=time_fn(),
        )
        return {
            "activity_id": activity_id,
            "conversation_id": conversation_id,
            "status": "failed",
            "error": message,
        }

    started_at = time_fn()
    db.update_activity_status(activity_id, "running", started_at=started_at)

    if router is not None and callable(getattr(router, "record_run_start", None)):
        router.record_run_start(
            scope_key=lease.scope_key,
            run_id=run_id,
            stored_session_id=conversation_id,
            turn_id=turn_id,
        )
    if callable(getattr(pool, "record_run_start", None)):
        await pool.record_run_start(
            conversation_id=conversation_id,
            run_id=run_id,
            stored_session_id=conversation_id,
            turn_id=turn_id,
        )

    frame_params = {
        "dovie_profile": target_profile_context,
        "agent_profile_id": target_profile_context["agent_profile_id"],
        "agent_profile_version_id": target_profile_context["agent_profile_version_id"],
        "runtime_scope_key": lease.scope_key,
        "parent_conversation_id": parent_conversation_id,
        "parent_activity_id": parent_activity_id,
        "dispatch_activity_id": activity_id,
        "files": files,
        "source": "agent_dispatch",
    }
    send_error = ""
    try:
        ok = await supervisor.send(
            lease.scope_key,
            RunStartFrame(
                run_id=run_id,
                turn_id=turn_id,
                stored_session_id=conversation_id,
                prompt=prompt,
                params=frame_params,
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
        db.update_activity_status(
            activity_id,
            "failed",
            result_summary=message,
            completed_at=time_fn(),
        )
        return {
            "activity_id": activity_id,
            "conversation_id": conversation_id,
            "status": "failed",
            "error": message,
        }

    return {
        "activity_id": activity_id,
        "conversation_id": conversation_id,
        "status": "running",
    }


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("worker.dispatch_agent_async cannot run inside an active event loop")


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
        return _ok(rid, _run_sync(dispatch_agent_async(params)))
    except ValueError as exc:
        return _err(rid, 4006, str(exc))
    except Exception as exc:
        return _err(rid, 5008, f"agent dispatch failed: {exc}")
