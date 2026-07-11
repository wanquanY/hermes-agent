# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import base64
import json
import queue

from dovie_extension.display_transcript import (
    sanitize_session_list_item,
    sanitize_transcript_messages,
)
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.message_history import load_conversation_history
from tui_gateway.services import run_control
from tui_gateway.services.profile_context import profile_context_for_params as _profile_context_for_params
from tui_gateway.services.workspace import (
    bind_session_workspace as _bind_session_workspace,
    normalize_session_cwd as _normalize_session_cwd,
    session_workspace_bindings_by_session_ids as _session_workspace_bindings_by_session_ids,
    workspace_for_session as _workspace_for_session,
    workspace_from_params as _workspace_from_params,
)

_server = bind_server_globals(globals())
_interrupt_work_queue: queue.SimpleQueue = queue.SimpleQueue()
_agent_interrupt_work_queue: queue.SimpleQueue = queue.SimpleQueue()
_INTERNAL_SESSION_LIST_SOURCES = ("tool", "cron")


def _normalized_conversation_kind(row: dict | None) -> str:
    kind = str(
        (row or {}).get("conversation_kind")
        or (row or {}).get("conversationKind")
        or ""
    ).strip().lower()
    return kind if kind in {"direct", "team"} else "direct"


def _interrupt_trace(message: str) -> None:
    if not is_truthy_value(os.environ.get("HERMES_INTERRUPT_TRACE")):
        return
    print(message, file=sys.stderr, flush=True)


def _interrupt_work_loop() -> None:
    while True:
        fn = _interrupt_work_queue.get()
        try:
            fn()
        except Exception as exc:
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] interrupt.work.failed error={type(exc).__name__}: {exc}",
            )


threading.Thread(
    target=_interrupt_work_loop,
    name="tui-session-interrupt-worker",
    daemon=True,
).start()


def _agent_interrupt_work_loop() -> None:
    while True:
        fn = _agent_interrupt_work_queue.get()
        try:
            fn()
        except Exception as exc:
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.work.failed error={type(exc).__name__}: {exc}",
            )


threading.Thread(
    target=_agent_interrupt_work_loop,
    name="tui-agent-interrupt-worker",
    daemon=True,
).start()


def _schedule_interrupt_work(fn) -> None:
    _interrupt_work_queue.put(fn)


def _schedule_agent_interrupt_work(fn) -> None:
    _agent_interrupt_work_queue.put(fn)


# ── Methods: session ─────────────────────────────────────────────────


def _stored_workspace(session_id: str) -> dict | None:
    try:
        return _workspace_for_session(session_id)
    except Exception:
        return None


def _requested_runtime_scope_key(params: dict | None = None) -> str:
    return str(
        (params or {}).get("runtime_scope_key")
        or (params or {}).get("runtimeScopeKey")
        or ""
    ).strip()


def _profile_db_from_params(params: dict | None = None):
    profile_context = _profile_context_for_params(params or {}) or {}
    hermes_home = str(profile_context.get("hermes_home") or "").strip()
    if not hermes_home:
        return None
    try:
        active_home = _resolve_home_path(hermes_home, fallback=hermes_home)
        default_home = _resolve_home_path(_hermes_home, fallback=_hermes_home)
        result = _get_session_db_for_home(
            active_home=active_home,
            default_home=default_home,
            default_db=_server._db,
            default_error=_server._db_error,
            db_by_home=_server._db_by_home,
            db_error_by_home=_server._db_error_by_home,
            logger=logger,
            create_if_missing=_current_method.get("") not in _READ_ONLY_DB_METHODS,
        )
        if active_home == default_home:
            _server._db = result.default_db
            _server._db_error = result.default_error
        return result.db
    except Exception:
        return None


def _db_for_session_request(params: dict | None, conversation_session_id: str = ""):
    stable = str(conversation_session_id or "").strip()
    if stable and _is_control_plane_conversation_session_id(stable):
        return _db_for_stable_session(stable)
    return _profile_db_from_params(params) or _get_db()


def _requested_runtime_executor(params: dict | None = None) -> str:
    params = params or {}
    profile = params.get("dovie_profile") or params.get("dovieProfile") or params.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    return str(
        params.get("runtime_executor")
        or params.get("runtimeExecutor")
        or profile.get("runtime_executor")
        or profile.get("runtimeExecutor")
        or ""
    ).strip()


def _requested_codex_home(params: dict | None = None) -> str:
    params = params or {}
    profile = params.get("dovie_profile") or params.get("dovieProfile") or params.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    return str(
        params.get("codex_home")
        or params.get("codexHome")
        or params.get("codexHomePath")
        or profile.get("codex_home")
        or profile.get("codexHome")
        or profile.get("codexHomePath")
        or ""
    ).strip()


def _requested_codex_extra_env(params: dict | None = None) -> dict:
    params = params or {}
    profile = params.get("dovie_profile") or params.get("dovieProfile") or params.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    for candidate in (
        params.get("codex_extra_env"),
        params.get("codexExtraEnv"),
        profile.get("codex_extra_env"),
        profile.get("codexExtraEnv"),
    ):
        if isinstance(candidate, dict) and candidate:
            return {str(k): str(v) for k, v in candidate.items() if v is not None}
    return {}


def _requested_codex_account_mode(params: dict | None = None) -> str:
    params = params or {}
    profile = params.get("dovie_profile") or params.get("dovieProfile") or params.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    return str(
        params.get("codex_account_mode")
        or params.get("codexAccountMode")
        or profile.get("codex_account_mode")
        or profile.get("codexAccountMode")
        or ""
    ).strip().lower()


def _is_byo_codex_agent(agent) -> bool:
    if agent is None:
        return False
    if str(getattr(agent, "api_mode", "") or "").strip() != "codex_app_server":
        return False
    try:
        from agent.codex_runtime import normalize_codex_account_mode

        account_mode = normalize_codex_account_mode(
            getattr(agent, "codex_account_mode", ""),
            extra_env=getattr(agent, "codex_extra_env", None),
        )
    except Exception:
        return False
    return account_mode == "byo"


def _requested_agent_profile_id(params: dict | None = None) -> str:
    return str(
        (params or {}).get("agent_profile_id")
        or (params or {}).get("agentProfileId")
        or ""
    ).strip()


def _requested_profile_version_id(params: dict | None = None) -> str:
    return str(
        (params or {}).get("agent_profile_version_id")
        or (params or {}).get("agentProfileVersionId")
        or ""
    ).strip()


def _requested_created_by_user_id(params: dict | None = None) -> str:
    return str(
        (params or {}).get("created_by_user_id")
        or (params or {}).get("createdByUserId")
        or (params or {}).get("created_by")
        or (params or {}).get("createdBy")
        or (params or {}).get("user_id")
        or (params or {}).get("userId")
        or ""
    ).strip()


def _ensure_session_create_conversation_participants(
    db,
    *,
    session_id: str,
    params: dict,
    runtime_scope_key: str,
) -> None:
    profile_id = _requested_agent_profile_id(params)
    scope = str(runtime_scope_key or "").strip()
    if not profile_id and scope.startswith("profile:"):
        profile_id = scope.split("profile:", 1)[1].strip()
    db.participants.ensure_user_participant(session_id, user_id="default")
    db.participants.ensure_agent_participant(
        session_id,
        agent_profile_id=profile_id,
        display_name=str(
            params.get("agent_profile_name")
            or params.get("agentProfileName")
            or params.get("profile_name")
            or params.get("profileName")
            or ""
        ).strip(),
        avatar=str(
            params.get("agent_profile_avatar")
            or params.get("agentProfileAvatar")
            or params.get("profile_avatar")
            or params.get("profileAvatar")
            or params.get("avatar")
            or ""
        ).strip(),
    )


def _session_owner_profile_id(params: dict, runtime_scope_key: str) -> str:
    profile_id = _requested_agent_profile_id(params)
    scope = str(runtime_scope_key or "")
    if not profile_id and scope.startswith("profile:"):
        profile_id = scope.split("profile:", 1)[1].strip()
    return profile_id


def _requested_tool_progress_mode(params: dict | None = None) -> str:
    raw = (
        (params or {}).get("tool_progress_mode")
        or (params or {}).get("toolProgressMode")
        or (params or {}).get("tool_progress")
        or (params or {}).get("toolProgress")
    )
    if raw is False:
        return "off"
    if raw is True:
        return "all"
    mode = str(raw or "").strip().lower()
    if mode in {"off", "new", "all", "verbose"}:
        return mode
    return _load_tool_progress_mode()


def _session_run_snapshot(runtime_sid: str, session: dict | None, db=None) -> dict:
    session = session or {}
    conversation_session_id = str(session.get("session_key") or runtime_sid or "")
    control_state = run_control.session_status(
        conversation_session_id,
        db=db,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    )
    running = bool(session.get("running") or control_state.get("running"))
    return {
        "running": running,
        "runtime_scope_key": str(control_state.get("runtime_scope_key") or session.get("active_runtime_scope_key") or conversation_session_id),
        "active_execution_session_id": runtime_sid or "",
        "active_run_id": str(session.get("active_run_id") or control_state.get("active_run_id") or "") if running else "",
        "active_turn_id": str(session.get("active_turn_id") or control_state.get("active_turn_id") or "") if running else "",
        "run_started_at": (session.get("run_started_at") or control_state.get("run_started_at") or 0) if running else 0,
        "run_updated_at": (session.get("run_updated_at") or control_state.get("run_updated_at") or 0) if running else 0,
        "last_event_seq": int(control_state.get("last_event_seq") or 0),
    }


def _live_sessions_by_stored_key() -> dict[str, tuple[str, dict]]:
    with _sessions_lock:
        snapshot = list(_sessions.items())
    live: dict[str, tuple[str, dict]] = {}
    for sid, session in snapshot:
        # Skip sessions whose teardown chokepoint has already run. Otherwise
        # the active-list count would monotonically grow until gateway
        # restart, as zombie rows accumulate between _finalize_session
        # marking them and the next idle-reap actually popping them.
        # Ported from upstream ae94ed172 review-driven fix; keys on
        # _finalized only (NOT on a stdio sentinel) so a standalone
        # `hermes --tui` session stays visible.
        if (session or {}).get("_finalized"):
            continue
        key = str((session or {}).get("session_key") or "")
        if not key:
            continue
        current = live.get(key)
        if current is None:
            live[key] = (sid, session)
            continue
        _current_sid, current_session = current
        current_rank = (
            1 if current_session.get("running") else 0,
            float(current_session.get("run_updated_at") or 0),
            float(current_session.get("run_started_at") or 0),
        )
        candidate_rank = (
            1 if session.get("running") else 0,
            float(session.get("run_updated_at") or 0),
            float(session.get("run_started_at") or 0),
        )
        if candidate_rank > current_rank:
            live[key] = (sid, session)
    return live


def _find_live_session_by_key(session_key: str) -> tuple[str, dict] | None:
    key = str(session_key or "").strip()
    if not key:
        return None
    with _sessions_lock:
        snapshot = list(_sessions.items())
    candidates = [
        (sid, session)
        for sid, session in snapshot
        if not (session or {}).get("_finalized")
        and str((session or {}).get("session_key") or "") == key
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            bool((item[1] or {}).get("running")),
            float((item[1] or {}).get("run_updated_at") or 0),
            float((item[1] or {}).get("run_started_at") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _live_scope_matches(session: dict, runtime_scope_key: str) -> bool:
    requested = str(runtime_scope_key or "").strip()
    if not requested:
        return True
    live_scope = str(
        session.get("runtime_scope_key")
        or session.get("active_runtime_scope_key")
        or ""
    ).strip()
    return not live_scope or live_scope == requested


def _is_empty_stored_conversation(row: dict) -> bool:
    """Return true only when a rich session row is explicitly content-empty.

    ``session.list`` can be backed by older tests or alternate DB shims that
    expose a minimal session row. Treating missing ``message_count`` /
    ``title`` / ``preview`` fields as empty makes the gateway silently hide
    real history rows. Only the canonical rich-row shape can prove that a
    stored row is an empty placeholder.
    """
    if not all(key in row for key in ("message_count", "title", "preview")):
        return False
    return (
        int(row.get("message_count") or 0) <= 0
        and not (row.get("title") or "").strip()
        and not (row.get("preview") or "").strip()
    )


def _latest_active_conversation_mission_projection(
    db,
    conversation_id: str,
    *,
    fallback_active_mission_id: str = "",
) -> dict:
    """Return the sidebar mission projection for a team conversation."""

    cid = str(conversation_id or "").strip()
    if not cid:
        return {
            "active_mission_id": str(fallback_active_mission_id or "").strip(),
            "mission_status": "",
        }
    active = []
    lister = getattr(db, "list_conversation_missions", None)
    if callable(lister):
        try:
            active = lister(cid, status="active") or []
        except TypeError:
            active = lister(conversation_id=cid, status="active") or []
        except Exception:
            active = []
    active = [mission for mission in active if isinstance(mission, dict)]
    active.sort(
        key=lambda mission: (
            float(mission.get("updated_at") or 0),
            float(mission.get("added_at") or 0),
            str(mission.get("mission_id") or ""),
        ),
        reverse=True,
    )
    latest = active[0] if active else {}
    active_mission_id = str(
        latest.get("mission_id")
        or fallback_active_mission_id
        or ""
    ).strip()
    mission_status = str(latest.get("status") or "").strip()
    return {
        "active_mission_id": active_mission_id,
        "mission_status": mission_status,
    }


def _is_team_mission_internal_session_row(
    db,
    row: dict,
    team_run_session_ids: set[str] | None = None,
) -> bool:
    session_ids = {
        str(row.get("id") or "").strip(),
        str(row.get("conversation_session_id") or row.get("conversationSessionId") or "").strip(),
        str(row.get("session_id") or row.get("sessionId") or "").strip(),
    }
    session_ids.discard("")
    for session_id in session_ids:
        if session_id.startswith("team:") and ":node:" in session_id:
            return True
        if team_run_session_ids is not None and session_id in team_run_session_ids:
            return True
    runtime_scope_key = str(
        row.get("runtime_scope_key")
        or row.get("runtimeScopeKey")
        or ""
    ).strip()
    if runtime_scope_key.startswith("team:") and ":node:" in runtime_scope_key:
        return True
    if team_run_session_ids is None:
        is_run_session = getattr(db, "is_team_mission_run_session", None)
        if callable(is_run_session) and any(is_run_session(session_id) for session_id in session_ids):
            return True
    return False


def _team_mission_session_list_item(db, row: dict, team_run_session_ids: set[str] | None = None) -> dict | None:
    """Overlay Hermes team-conversation identity onto its backing session row."""

    session_id = str(row.get("id") or "").strip()
    getter = getattr(db, "get_team_mission_conversation_by_session", None)
    conversation = getter(session_id) if callable(getter) and session_id else {}
    if conversation:
        conversation_id = str(conversation.get("conversation_id") or "").strip()
        conversation_session_id = str(conversation.get("conversation_session_id") or row.get("id") or "").strip()
        if not conversation_id or not conversation_session_id:
            return None
        updated_at = conversation.get("updated_at") or row.get("last_active") or row.get("started_at") or 0
        created_at = conversation.get("created_at") or row.get("started_at") or updated_at
        has_active_mission = getattr(db, "has_active_mission", None)
        running = bool(has_active_mission(conversation_id)) if callable(has_active_mission) else bool(row.get("running"))
        mission_projection = _latest_active_conversation_mission_projection(
            db,
            conversation_id,
            fallback_active_mission_id=str(conversation.get("active_mission_id") or "").strip(),
        )
        return {
            **row,
            "id": conversation_session_id,
            "conversation_session_id": conversation_session_id,
            "session_id": conversation_session_id,
            "session_kind": "team_mission",
            "conversation_kind": "team",
            "source": "team_mission",
            "conversation_id": conversation_id,
            "team_id": str(conversation.get("team_id") or "").strip(),
            "team_conversation_title": str(conversation.get("title") or "").strip(),
            "active_mission_id": mission_projection["active_mission_id"],
            "mission_id": mission_projection["active_mission_id"],
            "mission_status": mission_projection["mission_status"],
            "status": str(conversation.get("status") or "").strip(),
            "running": running,
            "title": str(conversation.get("title") or row.get("title") or "").strip(),
            "display_title": str(conversation.get("display_title") or conversation.get("title") or row.get("display_title") or row.get("preview") or "").strip(),
            "display_title_source": str(conversation.get("display_title_source") or "first_user_message").strip(),
            "preview": str(conversation.get("objective") or row.get("preview") or "").strip(),
            "workspace": {
                "id": str(conversation.get("workspace_id") or "").strip(),
                "path": str(conversation.get("workspace_path") or "").strip(),
                "kind": "local",
            },
            "started_at": created_at,
            "updated_at": updated_at,
        }

    if _is_team_mission_internal_session_row(db, row, team_run_session_ids):
        return None
    if (row.get("source") or "").strip().lower() == "team_mission":
        return None
    return row


def _request_agent_interrupt_async(sid: str, agent) -> None:
    if agent is None or not hasattr(agent, "interrupt"):
        _interrupt_trace(
            f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.skip sid={sid} has_agent={agent is not None}",
        )
        return

    def run_interrupt() -> None:
        started_at = time.time()
        _interrupt_trace(
            f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.begin sid={sid}",
        )
        try:
            agent.interrupt()
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.done sid={sid} elapsed_ms={int((time.time() - started_at) * 1000)}",
            )
        except Exception as exc:
            _interrupt_trace(
                f"[hermes] [tui_gateway] [interrupt-trace] agent.interrupt.failed sid={sid} error={type(exc).__name__}: {exc}",
            )

    _schedule_agent_interrupt_work(run_interrupt)


def _safe_subagent_attr(agent, name: str, fallback=None):
    try:
        value = getattr(agent, name, fallback)
    except Exception:
        return fallback
    return value if value is not None else fallback


def _active_child_agents(agent) -> list:
    if agent is None:
        return []
    lock = _safe_subagent_attr(agent, "_active_children_lock")
    try:
        if lock:
            with lock:
                return list(_safe_subagent_attr(agent, "_active_children", []) or [])
        return list(_safe_subagent_attr(agent, "_active_children", []) or [])
    except Exception:
        return []


def _iter_active_subagent_agents(agent, seen: set[int] | None = None):
    seen = seen or set()
    for child in _active_child_agents(agent):
        marker = id(child)
        if marker in seen:
            continue
        seen.add(marker)
        yield child
        yield from _iter_active_subagent_agents(child, seen)


def _subagent_task_index(child) -> int:
    value = _safe_subagent_attr(child, "_subagent_task_index", None)
    if isinstance(value, int):
        return max(0, value)
    subagent_id = str(_safe_subagent_attr(child, "_subagent_id", "") or "")
    parts = subagent_id.split("-")
    if len(parts) >= 3 and parts[0] == "sa":
        try:
            return max(0, int(parts[1]))
        except (TypeError, ValueError):
            pass
    return 0


def _emit_interrupted_subagent_completions(
    *,
    sid: str,
    session: dict,
    interrupted_run_id: str,
    interrupted_turn_id: str,
    completion_status: str,
) -> None:
    agent = session.get("agent")
    emitted: set[str] = set()
    status = str(completion_status or "interrupted").strip().lower() or "interrupted"
    if status in {"cancelled", "canceled"}:
        status = "cancelled"
    elif status not in {"interrupted", "failed", "timeout"}:
        status = "interrupted"
    timestamp = time.time()
    for child in _iter_active_subagent_agents(agent):
        subagent_id = str(_safe_subagent_attr(child, "_subagent_id", "") or "").strip()
        if not subagent_id or subagent_id in emitted:
            continue
        emitted.add(subagent_id)
        task_index = _subagent_task_index(child)
        task_count = _safe_subagent_attr(child, "_subagent_task_count", None)
        try:
            task_count = max(1, int(task_count or 1))
        except (TypeError, ValueError):
            task_count = 1
        delegate_call_id = str(_safe_subagent_attr(child, "_subagent_delegate_call_id", "") or "").strip()
        toolsets = _safe_subagent_attr(child, "_subagent_toolsets", None)
        if not isinstance(toolsets, list):
            toolsets = []
        payload = {
            "subagent_id": subagent_id,
            "parent_id": str(_safe_subagent_attr(child, "_parent_subagent_id", "") or ""),
            "depth": int(_safe_subagent_attr(child, "_subagent_tui_depth", 0) or 0),
            "task_index": task_index,
            "task_count": task_count,
            "goal": str(_safe_subagent_attr(child, "_subagent_goal", "") or ""),
            "dispatch_message": str(_safe_subagent_attr(child, "_subagent_goal", "") or ""),
            "agent_name": str(_safe_subagent_attr(child, "_subagent_name", "") or ""),
            "model": str(_safe_subagent_attr(child, "model", "") or ""),
            "role": str(_safe_subagent_attr(child, "_delegate_role", "") or "leaf"),
            "toolsets": [str(item) for item in toolsets],
            "status": status,
            "summary": "任务已终止",
            "run_id": interrupted_run_id,
            "turn_id": interrupted_turn_id,
            "timestamp": timestamp,
        }
        if delegate_call_id:
            payload["delegate_call_id"] = delegate_call_id
            payload["tool_call_id"] = delegate_call_id
        _emit("subagent.complete", sid, payload)


def _request_session_interrupt_side_effects_async(
    *,
    sid: str,
    session: dict,
    should_interrupt_agent: bool,
    interrupted_run_id: str,
    interrupted_turn_id: str,
    completion_status: str = "interrupted",
) -> None:
    def run_side_effects() -> None:
        if should_interrupt_agent:
            with session["history_lock"]:
                active_run_id = str(session.get("active_run_id") or "")
                active_turn_id = str(session.get("active_turn_id") or "")
                if (
                    (active_run_id and active_run_id != interrupted_run_id)
                    or (active_turn_id and active_turn_id != interrupted_turn_id)
                ):
                    _interrupt_trace(
                        "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.side_effects.skip_stale "
                        f"sid={sid} interrupted_run_id={interrupted_run_id or '-'} "
                        f"interrupted_turn_id={interrupted_turn_id or '-'} active_run_id={active_run_id or '-'} "
                        f"active_turn_id={active_turn_id or '-'}",
                    )
                    return
            _interrupt_trace(
                "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.side_effects.begin "
                f"sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'}",
            )
            _request_agent_interrupt_async(sid, session.get("agent"))
            try:
                from tools.approval import resolve_gateway_approval

                resolve_gateway_approval(session["session_key"], "deny", resolve_all=True)
            except Exception:
                pass
            _emit_interrupted_subagent_completions(
                sid=sid,
                session=session,
                interrupted_run_id=interrupted_run_id,
                interrupted_turn_id=interrupted_turn_id,
                completion_status=completion_status,
            )
            _emit(
                "message.complete",
                sid,
                {
                    "text": "",
                    "status": completion_status,
                    "run_id": interrupted_run_id,
                    "turn_id": interrupted_turn_id,
                },
            )
            _interrupt_trace(
                "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.side_effects.done "
                f"sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'}",
            )

    _schedule_interrupt_work(run_side_effects)


def _bounded_page_limit(value, *, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _encode_page_cursor(payload: dict | None) -> str:
    if not payload:
        return ""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_page_cursor(value) -> dict:
    token = str(value or "").strip()
    if not token:
        return {}
    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _message_page_info(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    prev_id = raw.get("prev_cursor_id")
    next_id = raw.get("next_cursor_id")
    return {
        "prevCursor": _encode_page_cursor({"id": prev_id}) if prev_id is not None else "",
        "nextCursor": _encode_page_cursor({"id": next_id}) if next_id is not None else "",
        "hasMoreBefore": bool(raw.get("hasMoreBefore")),
        "hasMoreAfter": bool(raw.get("hasMoreAfter")),
        "totalCount": int(raw.get("totalCount") or 0),
    }


def _display_history_page(db, session_id: str, hydrate: str, limit: int) -> tuple[list[dict], dict]:
    mode = hydrate if hydrate in {"full", "tail", "none"} else "full"
    if mode == "none":
        return [], {
            "prevCursor": "",
            "nextCursor": "",
            "hasMoreBefore": False,
            "hasMoreAfter": False,
            "totalCount": 0,
        }
    if mode == "tail":
        page = db.messages.page_as_conversation(
            session_id,
            direction="tail",
            limit=limit,
            include_ancestors=True,
        )
        return (
            sanitize_transcript_messages(_history_to_messages(page.get("messages") or [])),
            _message_page_info(page.get("pageInfo")),
        )
    display_history = load_conversation_history(
        db,
        session_id,
        include_ancestors=True,
        include_storage_metadata=True,
    )
    messages = sanitize_transcript_messages(_history_to_messages(display_history))
    page_info = {
        "prevCursor": "",
        "nextCursor": "",
        "hasMoreBefore": False,
        "hasMoreAfter": False,
        "totalCount": len(messages),
    }
    return messages, page_info


def _display_history_conversation(db, session_id: str) -> list[dict]:
    return load_conversation_history(
        db,
        session_id,
        include_ancestors=True,
        include_storage_metadata=True,
    )


def _page_live_history(history: list[dict], hydrate: str, limit: int) -> tuple[list[dict], dict]:
    mode = hydrate if hydrate in {"full", "tail", "none"} else "full"
    total = len(history)
    if mode == "none":
        page = []
    elif mode == "tail" and total > limit:
        page = history[-limit:]
    else:
        page = history
    return page, {
        "prevCursor": "",
        "nextCursor": "",
        "hasMoreBefore": mode == "tail" and total > len(page),
        "hasMoreAfter": False,
        "totalCount": total,
    }


def _live_session_payload(
    sid: str,
    target: str,
    session: dict,
    *,
    cols: int,
    cwd: str,
    workspace: dict,
    runtime_scope_key: str,
    hydrate: str,
    message_limit: int,
    db=None,
) -> dict:
    with session["history_lock"]:
        session["cols"] = cols
        session["transport"] = current_transport() or session.get("transport") or _stdio_transport
        session["cwd"] = cwd
        session["workspace"] = workspace
        if runtime_scope_key:
            session["runtime_scope_key"] = runtime_scope_key
        history = list(session.get("display_history_prefix") or []) + list(
            session.get("history") or []
        )
    page, page_info = _page_live_history(history, hydrate, message_limit)
    return {
        "session_id": sid,
        "resumed": target,
        "message_count": page_info.get("totalCount") or len(page),
        "messages": sanitize_transcript_messages(_history_to_messages(page)),
        "messagePageInfo": page_info,
        "info": _session_info(session.get("agent"), session),
        **_session_run_snapshot(sid, session, db=db),
    }


@method("session.create")
def _(rid, params: dict) -> dict:
    sid = uuid.uuid4().hex[:8]
    key = _new_session_key()
    cols = int(params.get("cols", 80))
    transient = bool(params.get("transient") or params.get("temporary") or params.get("ephemeral"))
    control_plane_only = bool(
        params.get("control_plane_only")
        or params.get("controlPlaneOnly")
        or params.get("defer_agent_build")
        or params.get("deferAgentBuild")
    )
    tool_progress_mode = _requested_tool_progress_mode(params)
    runtime_scope_key = _requested_runtime_scope_key(params)
    agent_context_mode = _agent_context_mode_from_params(params)
    # Seed history + create-time title: a client may open a session pre-populated
    # with a transcript and/or a title (classic TUI restore, dashboard import).
    seed_history = _coerce_seed_history(params.get("messages"))
    create_title = str(params.get("title") or "").strip()
    # Per-session model/effort/fast override shipped by the desktop composer on
    # session.create. Built INTO the agent (see _make_agent honoring
    # session["model_override"]) so the session starts on its own model without
    # a post-build /model switch. Never a global config write.
    create_model = str(params.get("model") or "").strip()
    create_provider = str(params.get("provider") or "").strip()
    create_runtime_executor = _requested_runtime_executor(params)
    create_codex_home = _requested_codex_home(params)
    create_codex_extra_env = _requested_codex_extra_env(params)
    create_codex_account_mode = _requested_codex_account_mode(params)
    session_model_override = None
    if create_model or create_provider or create_runtime_executor or create_codex_home or create_codex_extra_env or create_codex_account_mode:
        session_model_override = {
            "model": create_model,
            "provider": create_provider or None,
            "runtime_executor": create_runtime_executor or None,
            "codex_home": create_codex_home or None,
            "codex_extra_env": create_codex_extra_env or None,
            "codex_account_mode": create_codex_account_mode or None,
            "model_explicit": bool(create_model),
        }
    create_reasoning_override = None
    if _effort := str(params.get("reasoning_effort") or "").strip():
        try:
            from hermes_constants import parse_reasoning_effort

            create_reasoning_override = parse_reasoning_effort(_effort)
        except Exception:
            create_reasoning_override = None
    create_service_tier_override = "priority" if params.get("fast") else None
    # Opt-in eager teardown on transport disconnect (dovie sidecar / dashboard
    # embed). Consumed by _close_sessions_for_transport on the reaper path.
    close_on_disconnect = is_truthy_value(params.get("close_on_disconnect", False))
    try:
        cwd = _normalize_session_cwd(params.get("cwd"))
        workspace = _workspace_from_params(params, cwd)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    try:
        workspace = _bind_session_workspace(
            session_id=key,
            cwd=cwd,
            workspace=workspace,
        )
    except Exception as exc:
        return _err(rid, 5012, f"workspace bind failed: {exc}")
    db = _db_for_session_request(params, key)
    if db is None and control_plane_only:
        return _db_unavailable_error(rid, code=5000)
    # Honor the desktop composer's per-session model pick for the projected row
    # + control-plane response, instead of the global config default. The
    # control-plane session.create paints the conversation before any runtime
    # agent exists, so without this the row/sidebar/return briefly show the
    # global model until the first turn's switch lands. Falls back to the
    # global model when the client made no pick.
    model = create_model or _resolve_model()
    # Assemble the row's initial model_config so the runtime-worker subprocess
    # — which only reads the DB, never the sidecar's in-memory session dict —
    # can rebuild an agent with api_mode=codex_app_server. Without this the
    # worker builds an openai-codex codex_responses agent and dies on the
    # missing OAuth token check ("Provider 'openai-codex' is set in
    # config.yaml but no API key was found").
    codex_row_config: dict = {}
    if session_model_override:
        _re = str(session_model_override.get("runtime_executor") or "").strip()
        _ch = str(session_model_override.get("codex_home") or "").strip()
        _ce = session_model_override.get("codex_extra_env")
        _cam = str(session_model_override.get("codex_account_mode") or "").strip()
        _model_explicit = bool(session_model_override.get("model_explicit"))
        _prov = str(session_model_override.get("provider") or "").strip()
        if _re:
            codex_row_config["runtime_executor"] = _re
        if _ch:
            codex_row_config["codex_home"] = _ch
        if isinstance(_ce, dict) and _ce:
            codex_row_config["codex_extra_env"] = {
                str(k): str(v) for k, v in _ce.items() if v is not None
            }
        if _cam:
            codex_row_config["codex_account_mode"] = _cam
        if _re or _ch or _cam:
            codex_row_config["model_explicit"] = _model_explicit
        if _prov:
            codex_row_config["provider"] = _prov
    if db is not None:
        try:
            db.sessions.create(
                key,
                "tui",
                model=model,
                model_config=codex_row_config or None,
                transient=transient,
                runtime_scope_key=runtime_scope_key,
                session_kind="hermes_session",
                conversation_kind="direct",
                owner_agent_profile_id=_session_owner_profile_id(
                    params,
                    runtime_scope_key,
                ),
                owner_profile_version_id=_requested_profile_version_id(params),
            )
        except Exception as exc:
            return _err(rid, 5000, f"session create failed: {exc}")
        try:
            _ensure_session_create_conversation_participants(
                db,
                session_id=key,
                params=params,
                runtime_scope_key=runtime_scope_key,
            )
        except Exception as exc:
            logger.warning(
                "session.create participant auto-create skipped session_id=%s: %s",
                key,
                exc,
            )
    if control_plane_only:
        return _ok(
            rid,
            {
                "session_id": key,
                "conversation_session_id": key,
                "info": {
                    "model": model,
                    "tools": {},
                    "skills": {},
                    "cwd": cwd,
                    "workspace": workspace,
                    "lazy": True,
                    "transient": transient,
                    "control_plane_only": True,
                },
            },
        )

    _enable_gateway_prompts()
    ready = threading.Event()

    session_record = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
        "attached_images": [],
        "cols": cols,
        "cwd": cwd,
        "edit_snapshots": {},
        "history": seed_history,
        "history_lock": threading.Lock(),
        "history_version": 0,
        "image_counter": 0,
        "pending_title": create_title or None,
        "model_override": session_model_override,
        "create_reasoning_override": create_reasoning_override,
        "create_service_tier_override": create_service_tier_override,
        "close_on_disconnect": close_on_disconnect,
        "profile_context": _profile_context_for_params(params),
        "agent_context_mode": agent_context_mode,
        "runtime_scope_key": runtime_scope_key,
        "running": False,
        "active_run_id": None,
        "active_turn_id": None,
        "pending_turn": None,
        "run_started_at": 0,
        "run_updated_at": 0,
        "event_seq": 0,
        "interrupted_run_id": "",
        "interrupted_turn_id": "",
        "interrupt_seq": 0,
        "recalled_turn_ids": set(),
        "session_key": key,
        "show_reasoning": _load_show_reasoning(),
        "slash_worker": None,
        "tool_progress_mode": tool_progress_mode,
        "tool_started_at": {},
        "transport": current_transport() or _stdio_transport,
        "transient": transient,
        "workspace": workspace,
    }
    with _sessions_lock:
        _sessions[sid] = session_record

    if not control_plane_only:
        # Legacy TUI compatibility: return the lightweight session first, then
        # build the AIAgent shortly after response flush. Dovie/new run.*
        # callers should pass control_plane_only/defer_agent_build and let
        # run.submit lazily attach the runtime.
        def _deferred_build() -> None:
            with _sessions_lock:
                session = _sessions.get(sid)
            if session is not None:
                _start_agent_build(sid, session)

        build_timer = threading.Timer(0.05, _deferred_build)
        build_timer.daemon = True
        build_timer.start()

    with _sessions_lock:
        session = _sessions.get(sid)
    return _ok(
        rid,
        {
            "session_id": sid,
            "conversation_session_id": key,
            "message_count": len(seed_history),
            "messages": _history_to_messages(seed_history),
            "info": {
                # Reflect the per-session model override (desktop composer pick)
                # immediately so the client doesn't briefly clobber its sticky
                # pick with the global default before the build's session.info.
                "model": (
                    session_model_override.get("model")
                    if session_model_override
                    else _resolve_model()
                ),
                **(
                    {"provider": session_model_override["provider"]}
                    if session_model_override and session_model_override.get("provider")
                    else {}
                ),
                "tools": {},
                "skills": {},
                "cwd": cwd,
                "workspace": workspace,
                "lazy": True,
                "transient": transient,
                "control_plane_only": control_plane_only,
            },
        },
    )


@method("session.list")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5006)
    try:
        # Resume picker should surface human conversation sessions from every
        # user-facing surface — CLI, TUI, all gateway platforms (including new
        # ones not enumerated here), ACP adapter clients, webhook sessions,
        # custom `HERMES_SESSION_SOURCE` values, and older installs with
        # different source labels. We deny-list only noisy internal runtime
        # sources rather than allow-listing a fixed set of platform names
        # that goes stale whenever a new platform is added or a user names
        # their own source.
        #
        # ``tool`` rows are sub-agent runs. ``cron`` rows are scheduler
        # execution contexts; Dovie current-session/new-session result
        # bindings project their user-visible output into the target
        # conversation, so surfacing the raw cron session creates duplicate
        # sidebar conversations that begin with the internal cron prompt.
        deny = _INTERNAL_SESSION_LIST_SOURCES

        limit = _bounded_page_limit(params.get("limit"), default=200, maximum=200)
        cursor = _decode_page_cursor(params.get("cursor"))
        rows = [
            s
            for s in db.sessions.list(
                source=None,
                exclude_sources=tuple(deny),
                limit=limit + 1,
                page_cursor=cursor,
                order_by_last_active=True,
            )
            if str(s.get("source") or "").strip().lower() not in deny
        ]
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        team_run_session_ids = set()
        team_run_session_ids_getter = getattr(db, "team_mission_run_session_ids", None)
        if callable(team_run_session_ids_getter):
            team_run_session_ids = team_run_session_ids_getter([
                str(s.get("id") or "").strip()
                for s in page_rows
                if str(s.get("id") or "").strip()
            ])
        live_by_key = _live_sessions_by_stored_key()
        session_items = []
        for s in page_rows:
            s = _team_mission_session_list_item(db, s, team_run_session_ids)
            if s is None:
                continue
            live_sid, live_session = live_by_key.get(s["id"], ("", None))
            live_state = _session_run_snapshot(live_sid, live_session, db=db)
            running = bool(live_state.get("running") or s.get("running"))
            if not live_sid and _is_empty_stored_conversation(s):
                continue
            session_items.append(
                sanitize_session_list_item({
                    "id": s["id"],
                    "title": s.get("title") or "",
                    "display_title": s.get("display_title") or "",
                    "displayTitle": s.get("display_title") or "",
                    "display_title_source": s.get("display_title_source") or "",
                    "displayTitleSource": s.get("display_title_source") or "",
                    "preview": s.get("preview") or "",
                    "started_at": s.get("started_at") or 0,
                    "updated_at": s.get("updated_at") or s.get("last_active") or s.get("started_at") or 0,
                    "message_count": s.get("message_count") or 0,
                    "source": s.get("source") or "",
                    "workspace": s.get("workspace") or _stored_workspace(s["id"]),
                    "session_kind": s.get("session_kind") or "",
                    "conversation_kind": s.get("conversation_kind") or "direct",
                    "conversation_id": s.get("conversation_id") or "",
                    "team_id": s.get("team_id") or "",
                    "team_conversation_title": s.get("team_conversation_title") or "",
                    "active_mission_id": s.get("active_mission_id") or "",
                    "mission_id": s.get("mission_id") or "",
                    "mission_status": s.get("mission_status") or "",
                    "status": s.get("status") or "",
                    **live_state,
                    "running": running,
                })
            )
        next_cursor = ""
        if has_more and page_rows:
            cursor_payload = page_rows[-1].get("_page_cursor") or {
                "effective_last_active": page_rows[-1].get("last_active") or page_rows[-1].get("started_at") or 0,
                "started_at": page_rows[-1].get("started_at") or 0,
                "id": page_rows[-1].get("id") or "",
            }
            next_cursor = _encode_page_cursor(cursor_payload)
        return _ok(
            rid,
            {
                "sessions": session_items,
                "pageInfo": {
                    "nextCursor": next_cursor,
                    "hasMore": has_more,
                },
            },
        )
    except Exception as e:
        return _err(rid, 5006, str(e))


def _is_hidden_empty_index_draft(row: dict) -> bool:
    """A composer-paint placeholder the sidebar must NOT show.

    Control-plane ``session.create`` projects a session_index row for every new
    chat the moment the composer opens — before the user has typed anything — so
    the row appears with an empty title (rendered as "新会话"), zero messages,
    and no preview. These accumulate and re-appear on every launch (deleting them
    is futile; the next new-chat route re-creates one). ``session.list`` already
    hides such rows via ``_is_empty_stored_conversation``; mirror that here so the
    single-query ``session.index.list`` sidebar read is consistent. Keep the row
    when it is live (running / has an active run or runtime session) — that is a
    brand-new chat mid-first-turn whose content has not been persisted yet.
    """
    if not _is_empty_stored_conversation(row):
        return False
    if (
        row.get("running")
        or str(row.get("active_run_id") or "").strip()
        or str(row.get("active_execution_session_id") or "").strip()
    ):
        return False
    return True


def _session_index_list_item(row: dict) -> dict:
    """Map a control-plane session_index row to the desktop session list shape.

    P4 keeps the team sidebar shape single-source by always emitting the
    team-metadata keys. Plain chat sessions receive empty values while team
    rows receive the joined conversation and latest-active-mission projection.
    """
    session_kind = row.get("session_kind") or "hermes_session"
    conversation_kind = _normalized_conversation_kind(row)
    is_team_conversation = conversation_kind == "team"
    active_mission_id = (
        row.get("active_mission_id")
        or row.get("team_conversation_active_mission_id")
        or row.get("mission_id")
        or ""
    )
    item = {
        "id": row.get("session_id") or "",
        "title": row.get("title") or "",
        "display_title": row.get("title") or "",
        "displayTitle": row.get("title") or "",
        "preview": row.get("preview") or "",
        "started_at": row.get("started_at") or 0,
        "updated_at": row.get("updated_at") or row.get("started_at") or 0,
        "message_count": row.get("message_count") or 0,
        "source": row.get("source") or "",
        "transient": bool(row.get("transient")),
        "session_kind": session_kind,
        "conversation_kind": conversation_kind,
        "agentProfileId": row.get("owner_agent_profile_id") or "",
        "agent_profile_id": row.get("owner_agent_profile_id") or "",
        "agentProfileVersionId": row.get("owner_profile_version_id") or "",
        "runtimeScopeKey": row.get("runtime_scope_key") or "",
        "runtime_scope_key": row.get("runtime_scope_key") or "",
        "status": row.get("status") or "",
        "running": bool(row.get("running")),
        "waiting_approval": bool(row.get("waiting_approval")),
        "pending_approval_count": row.get("pending_approval_count") or 0,
        "active_activity_count": row.get("active_activity_count") or 0,
        "unread_completion_count": row.get("unread_completion_count") or 0,
        "active_run_id": row.get("active_run_id") or "",
        "active_execution_session_id": row.get("active_execution_session_id") or "",
        "conversation_id": row.get("conversation_id") or "",
        "team_id": row.get("team_id") or "",
        "team_conversation_title": row.get("team_conversation_title") or "",
        "mission_id": row.get("mission_id") or active_mission_id or "",
        "active_mission_id": active_mission_id,
        "mission_status": row.get("mission_status") or "",
        "workspace_binding": _session_index_workspace_binding_contract(
            row.get("workspace_binding")
        ),
        "team_context": _session_index_team_context_contract(row),
        "derived_state": _session_index_derived_state_contract(row),
    }
    if is_team_conversation:
        if active_mission_id:
            item["mission_id"] = active_mission_id
    # Conversation-architecture refactor (P2): team display context joined in
    # at read time. Only emit when present so plain-chat rows stay clean.
    if is_team_conversation and row.get("team_id"):
        team_name = row.get("team_name") or ""
        team_avatar = _safe_json_decode(row.get("team_avatar_json"))
        lead_profile_id = row.get("team_lead_profile_id") or ""
        lead_profile_name = row.get("team_lead_profile_name") or ""
        lead_profile_avatar = row.get("team_lead_profile_avatar") or ""
        member_count = int(row.get("team_member_count") or 0)
        leader_member = _safe_json_decode(row.get("team_leader_member_json")) or {}
        if not isinstance(leader_member, dict):
            leader_member = {}
        display_members = _safe_json_decode(row.get("team_display_members_json")) or []
        if not isinstance(display_members, list):
            display_members = []
        if leader_member and not display_members:
            display_members = [leader_member]
        team_block = {
            "id": row.get("team_id") or "",
            "name": team_name,
        }
        if team_avatar is not None:
            team_block["avatar"] = team_avatar
        if lead_profile_id:
            team_block["lead_agent_profile_id"] = lead_profile_id
            team_block["leadAgentProfileId"] = lead_profile_id
        if member_count:
            team_block["member_count"] = member_count
            team_block["memberCount"] = member_count
        if leader_member:
            team_block["leader_member"] = leader_member
            team_block["leaderMember"] = leader_member
        if display_members:
            team_block["display_members"] = display_members
            team_block["displayMembers"] = display_members
        item["team"] = team_block
        if team_name:
            item["team_name"] = team_name
            item["teamName"] = team_name
        if member_count:
            item["team_member_count"] = member_count
            item["teamMemberCount"] = member_count
        if lead_profile_name:
            item["lead_profile_name"] = lead_profile_name
            item["leadProfileName"] = lead_profile_name
        if lead_profile_avatar:
            item["lead_profile_avatar"] = lead_profile_avatar
            item["leadProfileAvatar"] = lead_profile_avatar
    if row.get("conversation_id"):
        objective = row.get("team_conversation_objective") or ""
        workspace_id = row.get("team_conversation_workspace_id") or ""
        workspace_path = row.get("team_conversation_workspace_path") or ""
        active_mission_id = row.get("team_conversation_active_mission_id") or ""
        if objective:
            item["objective"] = objective
        if workspace_id:
            item["workspace_id"] = workspace_id
            item["workspaceId"] = workspace_id
        if workspace_path:
            item["workspace_path"] = workspace_path
            item["workspacePath"] = workspace_path
        if active_mission_id and not item.get("active_mission_id"):
            item["active_mission_id"] = active_mission_id
    return item


def _session_index_workspace_binding_contract(binding: object) -> dict | None:
    if not isinstance(binding, dict):
        return None
    workspace_id = str(binding.get("workspace_id") or binding.get("id") or "").strip()
    workspace_path = str(
        binding.get("workspace_path")
        or binding.get("path")
        or ((binding.get("workspace") or {}).get("path") if isinstance(binding.get("workspace"), dict) else "")
        or ""
    ).strip()
    if not workspace_id and not workspace_path:
        return None
    return {
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
    }


def _session_index_team_context_contract(row: dict) -> dict | None:
    raw_context = row.get("team_context")
    if isinstance(raw_context, dict):
        context = {
            "team_id": str(raw_context.get("team_id") or "").strip(),
            "team_conversation_id": str(raw_context.get("team_conversation_id") or "").strip(),
            "mission_id": str(raw_context.get("mission_id") or "").strip(),
            "member_id": str(raw_context.get("member_id") or "").strip(),
        }
    else:
        context = {
            "team_id": str(row.get("team_context_team_id") or row.get("team_id") or "").strip(),
            "team_conversation_id": str(
                row.get("team_context_conversation_id") or row.get("conversation_id") or ""
            ).strip(),
            "mission_id": str(
                row.get("team_context_mission_id")
                or row.get("mission_id")
                or row.get("active_mission_id")
                or ""
            ).strip(),
            "member_id": str(row.get("team_context_member_id") or "").strip(),
        }
    return context if any(context.values()) else None


def _session_index_derived_state_contract(row: dict) -> dict:
    raw_state = row.get("derived_state")
    if isinstance(raw_state, dict):
        return {
            "running": bool(raw_state.get("running")),
            "waiting_approval": bool(raw_state.get("waiting_approval")),
            "terminal_status": (
                str(raw_state.get("terminal_status")).strip()
                if raw_state.get("terminal_status") is not None
                else None
            ) or None,
        }
    return {
        "running": bool(row.get("derived_running", row.get("running"))),
        "waiting_approval": bool(
            row.get("derived_waiting_approval", row.get("waiting_approval"))
        ),
        "terminal_status": (
            str(row.get("derived_terminal_status")).strip()
            if row.get("derived_terminal_status") is not None
            else None
        ) or None,
    }


def _session_index_workspace_bindings_for_rows(rows: list[dict]) -> dict[str, dict]:
    session_ids = [
        str(row.get("session_id") or row.get("id") or "").strip()
        for row in rows
        if isinstance(row, dict)
    ]
    session_ids = [session_id for session_id in dict.fromkeys(session_ids) if session_id]
    if not session_ids:
        return {}
    try:
        bindings = _session_workspace_bindings_by_session_ids(session_ids)
    except Exception:
        return {}
    return bindings if isinstance(bindings, dict) else {}


def _session_index_row_with_active_mission_running(db, row: dict) -> dict:
    item = dict(row or {})
    is_team_conversation = _normalized_conversation_kind(item) == "team"
    conversation_id = str(item.get("conversation_id") or "").strip()
    has_active_mission = getattr(db, "has_active_mission", None)
    if is_team_conversation and conversation_id and callable(has_active_mission):
        item["running"] = bool(has_active_mission(conversation_id))
    return item


def _safe_json_decode(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        import json
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


_SESSION_INDEX_RECONCILED = False


def _ensure_session_index_reconciled(db) -> None:
    """One-time backfill of the control-plane index from the source of truth
    (sessions table) per gateway process, on first sidebar read. Idempotent and
    preserves any live status already projected by write-time hooks."""
    global _SESSION_INDEX_RECONCILED
    if _SESSION_INDEX_RECONCILED:
        return
    db.session_index.reconcile()
    _SESSION_INDEX_RECONCILED = True


@method("session.index.list")
def _(rid, params: dict) -> dict:
    """Single-query sidebar read from the control-plane session_index.

    Replaces the per-profile fan-out of expensive session.list / activity calls
    with one indexed, keyset-paginated read (status is projected at write time).
    """
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5006)
    try:
        _ensure_session_index_reconciled(db)
        limit = _bounded_page_limit(params.get("limit"), default=200, maximum=200)
        cursor = _decode_page_cursor(params.get("cursor"))
        include_transient = is_truthy_value(
            params.get("include_transient")
            if params.get("include_transient") is not None
            else params.get("includeTransient")
        )
        requested_conversation_kind = str(
            params.get("conversation_kind")
            or params.get("conversationKind")
            or ""
        ).strip().lower()
        if requested_conversation_kind not in {"direct", "team"}:
            requested_conversation_kind = ""
        result = db.session_index.list(
            limit=limit,
            cursor=cursor or None,
            include_transient=include_transient,
            conversation_kind=requested_conversation_kind or None,
        )
        rows = [
            _session_index_row_with_active_mission_running(db, row)
            for row in (result.get("sessions") or [])
            if not requested_conversation_kind
            or _normalized_conversation_kind(row) == requested_conversation_kind
        ]
        visible_rows = [
            row
            for row in rows
            if not _is_hidden_empty_index_draft(row)
        ]
        workspace_bindings = _session_index_workspace_bindings_for_rows(visible_rows)
        enriched_rows = []
        for row in visible_rows:
            item = dict(row)
            session_id = str(item.get("session_id") or item.get("id") or "").strip()
            item["workspace_binding"] = workspace_bindings.get(session_id)
            enriched_rows.append(item)
        items = [
            sanitize_session_list_item(_session_index_list_item(row))
            for row in enriched_rows
        ]
        page = result.get("pageInfo") or {}
        next_cursor = _encode_page_cursor(page.get("nextCursor")) if page.get("nextCursor") else ""
        return _ok(
            rid,
            {
                "sessions": items,
                "pageInfo": {
                    "nextCursor": next_cursor,
                    "hasMore": bool(page.get("hasMore")),
                },
            },
        )
    except Exception as e:
        return _err(rid, 5006, str(e))


@method("session.most_recent")
def _(rid, params: dict) -> dict:
    """Return the most recent human-facing session id, or ``None``.

    Mirrors ``session.list``'s deny-list behaviour (drops internal
    runtime rows such as sub-agent and cron execution sessions). Used by
    TUI auto-resume when
    ``display.tui_auto_resume_recent`` is on; the field is also handy
    for any CLI tooling that wants "latest session" without paginating
    the full list.

    Contract: a ``{"session_id": null}`` result means "no eligible
    session found right now".  Errors are also folded into that
    null-result shape (and logged) so callers don't have to special-
    case JSON-RPC error envelopes for what is a normal "no answer".
    """
    db = _get_db()
    if db is None:
        return _ok(rid, {"session_id": None})
    try:
        deny = frozenset(_INTERNAL_SESSION_LIST_SOURCES)
        # Over-fetch by a generous bounded amount so heavy sub-agent
        # users (lots of recent ``tool`` rows) don't get a false
        # "no eligible session" answer.  ``session.list`` uses a
        # similar over-fetch strategy.
        rows = db.sessions.list(
            source=None,
            exclude_sources=tuple(deny),
            limit=200,
            order_by_last_active=True,
        )
        for row in rows:
            src = (row.get("source") or "").strip().lower()
            if src in deny:
                continue
            return _ok(
                rid,
                {
                    "session_id": row.get("id"),
                    "title": row.get("title") or "",
                    "display_title": row.get("display_title") or "",
                    "started_at": row.get("started_at") or 0,
                    "source": row.get("source") or "",
                },
            )
        return _ok(rid, {"session_id": None})
    except Exception:
        logger.exception("session.most_recent failed")
        return _ok(rid, {"session_id": None})


def _participant_view_for_resume(
    *,
    conversation_session_id: str,
    agent_context_mode: str,
    params: dict | None = None,
) -> str:
    """Return the participant_id the resuming runtime should hydrate its
    conversation history under.

    - team-leader runtime over a team conversation session → ``"leader"``
      (so leader sees its own assistant turns vs. other members' as
      observed user-side speech)
    - member-chat worker session → its ``member_id`` (member-chat sessions
      also materialize this view at write-time via
      sync_member_chat_conversation_view, so this lookup is mostly a
      belt-and-braces for any re-hydration paths)
    - all other sessions (plain chat, single-participant) → ``""`` and no
      projection is applied

    Caller passes the resolved agent_context_mode so we don't re-derive.
    """
    target = str(conversation_session_id or "").strip()
    mode = str(agent_context_mode or "").strip().lower()
    if mode == "team_leader":
        return "leader"
    if target.startswith("memberchat:"):
        # memberchat:<conv>:<member_id>
        rest = target[len("memberchat:"):]
        # split off conv prefix (which itself may contain ':' segments)
        # — the member id is the last colon-delimited segment.
        if ":" in rest:
            return rest.rsplit(":", 1)[-1].strip()
    return ""


@method("session.resume")
def _(rid, params: dict) -> dict:
    target = params.get("session_id", "")
    if not target:
        return _err(rid, 4006, "session_id required")
    db = _db_for_stable_session(target)
    if db is None:
        return _err(rid, 4007, "session not found")
    target, found = db.sessions.resolve_reference(target)
    if not found:
        return _err(rid, 4007, "session not found")
    # Context compression ends the current transcript session and forks a
    # continuation child that holds the post-compression turns (agent.session_id
    # rotates — see _sync_session_key_after_compress). Resuming the parent id
    # would reload the stale pre-compression transcript AND miss the live
    # session, which is keyed on the continuation tip. Re-anchor to the tip so
    # history loading, the live-session lookup, and the rebuilt agent all target
    # the session that actually holds the messages (#15000). Skipped for lazy
    # watch windows, which attach to the exact branch they were opened on.
    if found and not is_truthy_value(params.get("lazy", False)):
        try:
            tip = db.sessions.resolve_resume_id(target) or target
        except Exception:
            tip = target
        if tip and tip != target:
            target = tip
            found = db.sessions.get(target) or found
    existing_workspace = _stored_workspace(target)
    raw_cwd = params.get("cwd") or (existing_workspace or {}).get("cwd")
    workspace_params = params
    if not params.get("workspace") and existing_workspace:
        workspace_params = {**params, "workspace": existing_workspace}
    try:
        cwd = _normalize_session_cwd(raw_cwd)
        workspace = _workspace_from_params(workspace_params, cwd)
    except ValueError as exc:
        return _err(rid, 4002, str(exc))
    try:
        workspace = _bind_session_workspace(
            session_id=target,
            cwd=cwd,
            workspace=workspace,
        )
    except Exception as exc:
        return _err(rid, 5012, f"workspace bind failed: {exc}")
    hydrate = str(params.get("hydrate") or "full").strip().lower()
    runtime_scope_key = _requested_runtime_scope_key(params)
    message_limit = _bounded_page_limit(
        params.get("message_limit", params.get("messageLimit")),
        default=50,
        maximum=200,
    )
    try:
        cols = int(params.get("cols", 80))
    except (TypeError, ValueError):
        cols = 80
    with _session_resume_lock:
        live = _find_live_session_by_key(target)
        if live is not None:
            live_sid, live_session = live
            if not (params.get("_runtime_attach") and not _live_scope_matches(live_session, runtime_scope_key)):
                return _ok(
                    rid,
                    _live_session_payload(
                        live_sid,
                        target,
                        live_session,
                        cols=cols,
                        cwd=cwd,
                        workspace=workspace,
                        runtime_scope_key=runtime_scope_key,
                        hydrate=hydrate,
                        message_limit=message_limit,
                        db=db,
                    ),
                )

    sid = uuid.uuid4().hex[:8]
    _enable_gateway_prompts()
    try:
        db.sessions.reopen(target)
        history = load_conversation_history(db, target)
        # P1 participant-view projection: when this runtime is hydrating a
        # MULTI-PARTICIPANT conversation (the team leader reading a team
        # conversation that also contains member-chat mirrored replies, or
        # any other participant view), project the shared message log into
        # first-person view so the LLM doesn't conflate other participants'
        # assistant turns with its own. Single-participant chats pass
        # `viewer=""` and the projection is a no-op.
        agent_context_mode = _agent_context_mode_from_params(params)
        viewer_participant_id = _participant_view_for_resume(
            conversation_session_id=target,
            agent_context_mode=agent_context_mode,
            params=params,
        )
        if viewer_participant_id:
            from hermes_team_mission.domain.member_chat_projection import project_messages_for_viewer
            history = project_messages_for_viewer(history, viewer_participant_id)
        display_history = _display_history_conversation(db, target)
        display_history_prefix = display_history[
            : max(0, len(display_history) - len(history))
        ]
        messages, message_page_info = _display_history_page(db, target, hydrate, message_limit)
        profile_context = _profile_context_for_params(params)
        profile_tokens = _enter_profile_context(profile_context)
        tokens = _set_session_context(target, terminal_cwd=cwd)
        try:
            try:
                agent = _make_agent(
                    sid,
                    target,
                    session_id=target,
                    cwd=cwd,
                    agent_context_mode=agent_context_mode,
                    profile_context=profile_context,
                )
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc):
                    raise
                try:
                    agent = _make_agent(
                        sid,
                        target,
                        session_id=target,
                        agent_context_mode=agent_context_mode,
                    )
                except TypeError as nested_exc:
                    if "unexpected keyword argument" not in str(nested_exc):
                        raise
                    agent = _make_agent(sid, target, session_id=target)
        finally:
            _clear_session_context(tokens)
            _leave_profile_context(profile_tokens)
    except Exception as e:
        logger.warning(
            "[dovie-session-resume] failed session_id=%s hydrate=%s runtime_scope_key=%s db_type=%s error=%s",
            target,
            hydrate,
            runtime_scope_key,
            type(db).__name__,
            e,
        )
        return _err(rid, 5000, f"resume failed: {e}")

    with _session_resume_lock:
        live = _find_live_session_by_key(target)
        if live is not None:
            live_sid, live_session = live
            if not (params.get("_runtime_attach") and not _live_scope_matches(live_session, runtime_scope_key)):
                try:
                    if hasattr(agent, "close"):
                        agent.close()
                except Exception:
                    pass
                return _ok(
                    rid,
                    _live_session_payload(
                        live_sid,
                        target,
                        live_session,
                        cols=cols,
                        cwd=cwd,
                        workspace=workspace,
                        runtime_scope_key=runtime_scope_key,
                        hydrate=hydrate,
                        message_limit=message_limit,
                        db=db,
                    ),
                )
        try:
            _init_session(
                sid,
                target,
                agent,
                history,
                cols=cols,
                cwd=cwd,
                    workspace=workspace,
                    profile_context=profile_context,
                    agent_context_mode=agent_context_mode,
                )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            _init_session(sid, target, agent, history, cols=cols)
        with _sessions_lock:
            if sid in _sessions:
                _sessions[sid]["runtime_scope_key"] = runtime_scope_key
                _sessions[sid]["display_history_prefix"] = display_history_prefix
    with _sessions_lock:
        session = _sessions.get(sid)
    return _ok(
        rid,
        {
            "session_id": sid,
            "resumed": target,
            "message_count": message_page_info.get("totalCount") or len(messages),
            "messages": messages,
            "messagePageInfo": message_page_info,
            "info": (
                _session_info(agent, session)
                if session is not None
                else _session_info(agent)
            ),
            **_session_run_snapshot(sid, session, db=db),
        },
    )


@method("session.delete")
def _(rid, params: dict) -> dict:
    """Delete a stored session and its on-disk transcript files.

    Used by the TUI resume picker (``d`` key) so users can prune old
    sessions without dropping to the CLI.  Refuses to delete a session
    that is currently active in this gateway process — those rows are
    still being written to and removing them out from under the live
    agent corrupts message ordering and trips FK constraints when the
    next message append flushes.
    """
    target = params.get("session_id", "")
    if not target:
        return _err(rid, 4006, "session_id required")
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5036)
    run_state = run_control.session_status(
        target,
        db=db,
        current_gateway_instance_id=_GATEWAY_INSTANCE_ID,
    )
    if run_state.get("running"):
        return _err(rid, 4023, "cannot delete a session with an active run")
    # Block deletion of any session currently bound to a live TUI session
    # in this process.  The picker hides the active session anyway, but a
    # racing caller could still target it.  Snapshot via ``list(...)``
    # because ``_sessions`` is mutated by concurrent RPCs on the thread
    # pool — iterating the dict directly can raise ``RuntimeError:
    # dictionary changed size during iteration``.  If even the snapshot
    # raises, fail closed (refuse the delete) rather than fail open.
    try:
        with _sessions_lock:
            snapshot = list(_sessions.values())
    except Exception as e:
        return _err(rid, 5036, f"could not enumerate active sessions: {e}")
    active = {s.get("session_key") for s in snapshot if s.get("session_key")}
    if target in active:
        return _err(rid, 4023, "cannot delete an active session")
    sessions_dir = Path(get_hermes_home()) / "sessions"
    try:
        deletion = db.sessions.delete(target, sessions_dir=sessions_dir)
    except Exception as e:
        return _err(rid, 5036, f"delete failed: {e}")
    if not deletion.session_deleted:
        # The sessions row is gone but session_index may still carry an
        # orphan — typical for a session whose creation flow failed mid-way
        # (e.g. a model-switch error terminates the run before any message
        # is persisted; session_index already saw the session create event,
        # the sessions table never received any inserts). The sidebar reads
        # session_index, so the orphan reappears on every refresh and the
        # user can never delete it. Same pattern as the team-conversation
        # orphan case fixed earlier. Sweep the index row too and report
        # success so the client treats it as deleted (it IS deleted — the
        # only state that survived was the index row).
        if deletion.index_deleted:
            return _ok(rid, {"deleted": target, "via": "session_index_cleanup"})
        return _err(rid, 4007, "session not found")
    return _ok(rid, {"deleted": target})


@method("session.title")
def _(rid, params: dict) -> dict:
    db = _get_db()
    if db is None:
        return _db_unavailable_error(rid, code=5007)
    requested = str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or ""
    ).strip()
    if not requested:
        return _err(rid, 4006, "session_id required")

    _runtime_sid, session = _resolve_runtime_session(requested)
    key = str((session or {}).get("session_key") or requested).strip()
    title = (params.get("title", "") or "").strip() if "title" in params else None
    if title is not None and not title:
        return _err(rid, 4021, "title required")

    if not session:
        try:
            key, stored_row = db.sessions.resolve_reference(key)
            if not stored_row:
                return _err(rid, 4007, "session not found")
        except Exception as e:
            return _err(rid, 5007, str(e))

    if "title" not in params:
        fallback = (session or {}).get("pending_title") or ""
        try:
            resolved_title = db.sessions.get_title(key) or ""
            if fallback:
                if db.sessions.set_title(key, fallback):
                    if session:
                        session["pending_title"] = None
                    resolved_title = fallback
                else:
                    existing_row = db.sessions.get(key)
                    existing_title = str((existing_row or {}).get("title") or "").strip()
                    if existing_title == fallback:
                        if session:
                            session["pending_title"] = None
                        resolved_title = fallback
                    elif not resolved_title:
                        resolved_title = fallback
            elif resolved_title:
                if session:
                    session["pending_title"] = None
        except Exception:
            resolved_title = fallback
        return _ok(
            rid,
            {
                "title": resolved_title,
                "session_key": key,
            },
        )
    try:
        if db.sessions.set_title(key, title):
            if session:
                session["pending_title"] = None
            return _ok(rid, {"pending": False, "title": title})
        # rowcount == 0 can mean "same value" as well as "missing row".
        # Queue only when the session row truly does not exist yet.
        existing_row = db.sessions.get(key)
        if existing_row:
            if session:
                session["pending_title"] = None
            return _ok(
                rid,
                {
                    "pending": False,
                    "title": (existing_row.get("title") or title),
                },
            )
        if not session:
            return _err(rid, 4007, "session not found")
        session["pending_title"] = title
        return _ok(rid, {"pending": True, "title": title})
    except ValueError as e:
        return _err(rid, 4022, str(e))
    except Exception as e:
        return _err(rid, 5007, str(e))


@method("session.usage")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    agent = session.get("agent")
    key = str(session.get("session_key") or params.get("session_id") or "")
    updated_at = int(time.time() * 1000)
    if agent is None:
        usage = {"calls": 0, "input": 0, "output": 0, "total": 0}
        provider = "unknown"
        model = "unknown"
    else:
        usage = _get_usage(agent)
        provider = getattr(agent, "provider", None) or "unknown"
        model = str(usage.get("model") or getattr(agent, "model", None) or "unknown")

    model_usage_entry = {
        "provider": provider,
        "model": model,
        "count": int(usage.get("calls") or 0),
        "totals": {
            "input": int(usage.get("input") or usage.get("prompt") or 0),
            "output": int(usage.get("output") or usage.get("completion") or 0),
            "cacheRead": int(usage.get("cache_read") or 0),
            "cacheWrite": int(usage.get("cache_write") or 0),
            "reasoning": int(usage.get("reasoning") or 0),
            "image": int(usage.get("image") or 0),
            "totalTokens": int(usage.get("total") or 0),
            "totalCost": float(usage.get("cost_usd") or 0),
        },
    }
    if _is_byo_codex_agent(agent):
        model_usage_entry["byo"] = True

    structured = {
        "updatedAt": updated_at,
        "sessions": [
            {
                "key": key,
                "usage": {
                    "modelUsage": [model_usage_entry]
                },
            }
        ],
    }
    structured.update(usage)
    return _ok(
        rid,
        structured,
    )


@method("session.status")
def _(rid, params: dict) -> dict:
    from hermes_constants import display_hermes_home

    requested = str(params.get("session_id") or params.get("conversation_session_id") or "").strip()
    if not requested:
        return _err(rid, 4006, "session_id required")
    runtime_sid, session = _resolve_runtime_session(requested)
    key = str((session or {}).get("session_key") or requested)
    agent = (session or {}).get("agent")
    db = _db_for_session_request(params, key)
    meta_title = ""
    meta_started_at = 0.0
    meta_updated_at = 0.0
    stored = None
    if db and key:
        try:
            key, stored = db.sessions.resolve_reference(key)
            if stored:
                meta_title = str(stored.get("title") or "")
                meta_started_at = float(stored.get("started_at") or 0)
                meta_updated_at = float(stored.get("updated_at") or 0)
        except Exception as exc:
            return _err(rid, 5007, str(exc))
    if db and not stored and session is None:
        return _err(rid, 4007, "session not found")

    def _dt(value, fallback: datetime | None = None) -> datetime:
        if value:
            try:
                return datetime.fromtimestamp(float(value))
            except Exception:
                pass
        return fallback or datetime.now()

    created = _dt(meta_started_at)
    updated = created
    if meta_updated_at:
        updated = _dt(meta_updated_at, created)

    usage = _get_usage(agent) if agent is not None else {}
    provider = getattr(agent, "provider", None) or "unknown"
    model = getattr(agent, "model", None) or "(unknown)"
    lines = [
        "Hermes TUI Status",
        "",
        f"Session ID: {key}",
        f"Path: {display_hermes_home()}",
    ]
    title = (meta_title or "").strip()
    if title:
        lines.append(f"Title: {title}")
    lines.extend(
        [
            f"Model: {model} ({provider})",
            f"Created: {created.strftime('%Y-%m-%d %H:%M')}",
            f"Last Activity: {updated.strftime('%Y-%m-%d %H:%M')}",
            f"Tokens: {int(usage.get('total') or 0):,}",
            f"Agent Running: {'Yes' if (session or {}).get('running') else 'No'}",
        ]
    )
    return _ok(
        rid,
        {
            "output": "\n".join(lines),
            "session_id": runtime_sid,
            "conversation_session_id": key,
            **_session_run_snapshot(runtime_sid, session or {"session_key": key}, db=db),
        },
    )


@method("session.compress")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    if session.get("running"):
        return _err(
            rid, 4009, "session busy — /interrupt the current turn before /compress"
        )
    sid = params.get("session_id", "")
    focus_topic = str(params.get("focus_topic", "") or "").strip()
    try:
        from agent.manual_compression_feedback import summarize_manual_compression
        from agent.model_metadata import estimate_request_tokens_rough

        with session["history_lock"]:
            before_messages = list(session.get("history", []))
            history_version = int(session.get("history_version", 0))
        before_count = len(before_messages)
        _agent = session["agent"]
        _sys_prompt = getattr(_agent, "_cached_system_prompt", "") or ""
        _tools = getattr(_agent, "tools", None) or None
        before_tokens = (
            estimate_request_tokens_rough(
                before_messages, system_prompt=_sys_prompt, tools=_tools
            )
            if before_count
            else 0
        )

        if before_count >= 4:
            focus_suffix = f', focus: "{focus_topic}"' if focus_topic else ""
            _status_update(
                sid,
                "compressing",
                f"⠋ compressing {before_count} messages "
                f"(~{before_tokens:,} tok){focus_suffix}…",
            )

        try:
            removed, usage = _compress_session_history(
                session,
                focus_topic,
                approx_tokens=before_tokens,
                before_messages=before_messages,
                history_version=history_version,
            )
            with session["history_lock"]:
                messages = list(session.get("history", []))
            after_count = len(messages)
            # Re-read system prompt + tools after compression — _compress_context
            # may have rebuilt the system prompt (_cached_system_prompt=None).
            _sys_prompt_after = (
                getattr(_agent, "_cached_system_prompt", "") or _sys_prompt
            )
            _tools_after = getattr(_agent, "tools", None) or _tools
            after_tokens = (
                estimate_request_tokens_rough(
                    messages,
                    system_prompt=_sys_prompt_after,
                    tools=_tools_after,
                )
                if after_count
                else 0
            )
            agent = session["agent"]
            _sync_session_key_after_compress(sid, session)
            summary = summarize_manual_compression(
                before_messages, messages, before_tokens, after_tokens
            )
            info = _session_info(agent, session)
            _emit("session.info", sid, info)
            return _ok(
                rid,
                {
                    "status": "compressed",
                    "removed": removed,
                    "before_messages": before_count,
                    "after_messages": after_count,
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "summary": summary,
                    "usage": usage,
                    "info": info,
                    "messages": messages,
                },
            )
        finally:
            # Always clear the pinned compressing status so the bar
            # reverts to neutral whether compaction succeeded, was a
            # no-op, or raised.
            _status_update(sid, "ready")
    except Exception as e:
        return _err(rid, 5005, str(e))


@method("session.save")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err

    agent = session["agent"]
    # Mirror the classic CLI /save: snapshot under the Hermes profile home
    # (~/.hermes/sessions/saved/) rather than the project/workspace CWD, and
    # include the system prompt so the export matches the dashboard save.
    saved_dir = get_hermes_home() / "sessions" / "saved"
    try:
        saved_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return _err(rid, 5011, f"failed to create save directory {saved_dir}: {e}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = saved_dir / f"hermes_conversation_{timestamp}.json"

    with session["history_lock"]:
        messages = list(session.get("history", []))

    session_id = getattr(agent, "session_id", None) or session.get("session_key") or ""
    # Prefer the agent's session_start datetime (matches the classic CLI export);
    # fall back to the gateway session's created_at timestamp.
    agent_start = getattr(agent, "session_start", None)
    if isinstance(agent_start, datetime):
        session_start = agent_start.isoformat()
    else:
        created_at = session.get("created_at")
        session_start = (
            datetime.fromtimestamp(created_at).isoformat()
            if isinstance(created_at, (int, float))
            else ""
        )

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model": getattr(agent, "model", ""),
                    "session_id": session_id,
                    "session_start": session_start,
                    "system_prompt": getattr(agent, "_cached_system_prompt", "") or "",
                    "messages": messages,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        return _ok(rid, {"file": str(path)})
    except Exception as e:
        return _err(rid, 5011, str(e))


@method("session.close")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "")
    runtime_sid = sid
    with _sessions_lock:
        session = _sessions.pop(runtime_sid, None)
    if not session and sid:
        try:
            with _sessions_lock:
                snapshot = list(_sessions.items())
        except Exception:
            snapshot = []
        for candidate_sid, candidate in snapshot:
            if candidate.get("session_key") == sid:
                runtime_sid = candidate_sid
                with _sessions_lock:
                    session = _sessions.pop(candidate_sid, None)
                break
    if not session:
        return _ok(rid, {"closed": False})
    _finalize_session(session)
    try:
        from tools.approval import unregister_gateway_notify

        unregister_gateway_notify(session["session_key"])
    except Exception:
        pass
    try:
        agent = session.get("agent")
        if agent and hasattr(agent, "close"):
            agent.close()
    except Exception:
        pass
    try:
        worker = session.get("slash_worker")
        if worker:
            worker.close()
    except Exception:
        pass
    return _ok(
        rid,
        {
            "closed": True,
            "session_id": runtime_sid,
            "conversation_session_id": session.get("session_key") or sid,
        },
    )
