"""Phase 5c.2 — real ``AgentRunner`` binding for the run_worker subprocess.

Wires the new ``WorkerRunBackend`` to ``methods/prompt._execute_prompt_submit``
inside the worker process. Three responsibilities, each addressing a
hazard the Plan agent investigation surfaced
(see commit history around Phase 5c.2):

1. ``setup_worker_environment()`` — neutralizes the legacy
   ``_stdio_transport`` so stray ``write_json`` / ``_emit`` calls do
   NOT leak JSON-RPC envelopes onto the protocol pipe that the
   supervisor decodes.
2. ``_ensure_worker_session(frame)`` — materializes the
   ``_sessions[sid]`` record the legacy code path expects. The main
   sidecar's ``session.create`` only updates the main process's
   ``_sessions`` dict; the worker has its own empty registry. Agent
   construction is deliberately left to ``_execute_prompt_submit`` so
   turn-scoped toolset overrides can be applied before ``AIAgent`` is
   built.
3. ``run_agent(frame, cancel_event)`` — the ``AgentRunner`` itself.
   Sets up a cancel watcher, invokes ``_execute_prompt_submit``, and
   **blocks until the agent's background thread truly finishes**
   (legacy ``_execute_prompt_submit`` spawns a thread internally and
   returns immediately — the runner must NOT return early, or the
   ``WorkerPublishBridge`` will uninstall mid-stream and event/
   terminal frames will race).

Phase 5c.2 deliberately leaves several follow-ups for Phase 5d / 6:
- MCP discovery (``discover_mcp_tools``) is not called — non-MCP
  tools still work; MCP-using agents will see a smaller toolset.
- Cron ticker is not started here (lives in the main sidecar).
"""

from __future__ import annotations

import io
import logging
import threading
import time
import uuid
from typing import Any, Optional

from tui_gateway.run_worker import RunStartFrame
from tui_gateway.services.profile_context import profile_context_for_params
from tui_gateway.services.workspace import session_workspace_run_context

_log = logging.getLogger(__name__)


# Idempotent env setup — invoked from ``_build_default_backend`` at
# worker bootstrap. ``_setup_lock`` guards repeated calls during tests.
_setup_lock = threading.Lock()
_setup_done = False


class _NoopTransport:
    """Stand-in for ``_stdio_transport`` inside the worker. Every legacy
    ``write_json`` / ``_emit`` invocation that would otherwise dump a
    JSON-RPC envelope to the protocol pipe becomes a successful no-op,
    so the supervisor's stdout decoder never sees a malformed line.

    ``write`` returns True because ``write_json`` / ``_emit`` consult
    the boolean to decide whether to retry; True means "delivered"."""

    def write(self, obj: Any) -> bool:  # noqa: D401 — single-method protocol
        return True

    def is_connected(self) -> bool:
        return True


def setup_worker_environment() -> None:
    """Import the legacy gateway server module and neutralize its
    stdout transport. Safe to call multiple times — only the first
    call has effect.

    Importing ``tui_gateway.server`` triggers a chain of side effects
    that the agent code path depends on:
      - ``HERMES_HOME`` is resolved from the worker's env (already
        set by ``WorkerSupervisor._spawn_locked``)
      - ``@method`` registry gets populated transitively
      - ``_real_stdout`` is captured (the worker's true stdout fd —
        ALSO the protocol pipe; that's what we then neutralize)

    Importing ``tui_gateway.methods.prompt`` and
    ``tui_gateway.methods.session`` populates the ``@method`` registry
    with the entries we'll dispatch through.
    """
    global _setup_done
    with _setup_lock:
        if _setup_done:
            return
        from tui_gateway import server as _server  # noqa: F401 — side-effect import
        # Must NOT touch ``_real_stdout`` directly: server.py captures
        # it before the swap so any later reassign there is too late.
        # Replace the transport instance instead.
        _server._stdio_transport = _NoopTransport()  # type: ignore[assignment]
        from tui_gateway.services.worker_db_proxy import get_default_worker_db_proxy

        db_proxy = get_default_worker_db_proxy()
        if db_proxy is not None:
            # The worker process must not materialize SessionDB. Keep the
            # legacy resolver shape but return the IPC proxy everywhere the
            # prompt/run-control stack asks for a DB handle.
            def _worker_db_for_stable_session(stable_session_id: str):
                return db_proxy.scoped(stable_session_id)

            _server._db = db_proxy
            _server._get_db = lambda: db_proxy  # type: ignore[assignment]
            _server._db_for_stable_session = _worker_db_for_stable_session  # type: ignore[assignment]

        from tui_gateway.methods import prompt as _prompt  # noqa: F401
        from tui_gateway.methods import session as _session  # noqa: F401
        _setup_done = True
        _log.info("[agent-runner] worker environment set up")


def _run_context_from_frame(frame: RunStartFrame) -> Any:
    params = frame.params if isinstance(frame.params, dict) else {}
    payload = params.get("run_context_json") or params.get("runContextJson")
    if payload is None:
        return None
    try:
        from hermes_team_mission.domain.run_context import RunContext

        return RunContext.from_payload(payload)
    except Exception as exc:
        _log.warning(
            "[agent-runner] run_context_json parse failed stored_session=%s: %s",
            frame.stored_session_id,
            exc,
        )
        return None


def _should_project_member_perspective(run_context: Any) -> bool:
    if run_context is None:
        return False
    activity_kind = str(getattr(run_context, "activity_kind", "") or "").strip()
    participant_id = str(getattr(run_context, "participant_id", "") or "").strip()
    conversation_session_id = str(getattr(run_context, "conversation_session_id", "") or "").strip()
    execution_scope_key = str(getattr(run_context, "execution_scope_key", "") or "").strip()
    if activity_kind == "member_chat":
        return True
    if activity_kind in {"mission", "team_dispatch"}:
        return True
    if participant_id.startswith(("leader:", "member:")):
        return True
    if conversation_session_id.startswith("team-session-team-conversation-"):
        return True
    if execution_scope_key.startswith(("team:", "member-chat:")):
        return True
    return False


def _ensure_worker_session(frame: RunStartFrame) -> tuple[str, dict]:
    """Materialize a ``_sessions[sid]`` record for the run.

    The main sidecar's ``session.create`` only touched its OWN
    ``_sessions`` dict — the worker has no record. We build one inline
    with the same field shape ``methods/session.py:780-819`` produces,
    keyed by a fresh runtime sid, with ``session_key`` bound to the
    frame's ``stored_session_id`` (the stable id the agent uses for
    DB row lookups).

    The AIAgent build is intentionally NOT started here. The prompt
    handler applies turn-scoped toolset overrides before calling
    ``_start_agent_build``; starting the build in this worker bootstrap
    path races that override and can expose the default tool surface to
    team leader runs.
    """
    from tui_gateway import server as _server

    runtime_sid = uuid.uuid4().hex[:8]
    params = frame.params if isinstance(frame.params, dict) else {}
    run_context = _run_context_from_frame(frame)
    try:
        from agent.activity_event_bus import get_default_activity_event_bus

        activity_event_bus = get_default_activity_event_bus()
    except Exception:
        activity_event_bus = None

    workspace_context = session_workspace_run_context(frame.stored_session_id, params)
    cwd = str(workspace_context.get("cwd") or "").strip() or None
    workspace = (
        workspace_context.get("workspace")
        if isinstance(workspace_context.get("workspace"), dict)
        else {}
    )
    runtime_scope_key = str(
        params.get("runtime_scope_key")
        or params.get("runtimeScopeKey")
        or ""
    ).strip()
    agent_profile_id = str(
        params.get("agent_profile_id")
        or params.get("agentProfileId")
        or ""
    ).strip()
    transient = bool(
        params.get("transient")
        or params.get("temporary")
        or params.get("ephemeral")
    )
    # BUG-1 fix: normalize the worker session's profile_context through
    # ``profile_context_for_params`` so downstream readers (notably
    # ``enter_profile_context`` at prompt.py:600 / server.py:368) see the
    # snake_case shape they expect. The previous code stored the raw
    # ``dovie_profile`` dict whose top-level keys are camelCase
    # (``hermesHomePath`` / ``runtimeScopeKey``); ``enter_profile_context``
    # only reads ``hermes_home`` (snake_case), so the ContextVar
    # ``_HERMES_HOME_OVERRIDE`` was never set — every ``get_hermes_home()``
    # call inside the worker fell through to the ENV/default branch and
    # any code calling ``active_hermes_home()`` returned the wrong profile.
    # See docs/Dovie/agent-architecture/team-at-member-diagnostic-audit.md.
    raw_dovie_profile = params.get("dovie_profile") if isinstance(params.get("dovie_profile"), dict) else None
    profile_context: Optional[dict] = profile_context_for_params(params)
    if isinstance(profile_context, dict):
        agent_profile_id = agent_profile_id or str(profile_context.get("id") or "").strip()
        runtime_scope_key = runtime_scope_key or str(profile_context.get("runtime_scope_key") or "").strip()
    if not runtime_scope_key and agent_profile_id:
        runtime_scope_key = f"profile:{agent_profile_id}"

    # Match the legacy field set so every code path the prompt handler
    # touches finds what it expects. Don't trim — missing fields like
    # ``history_lock`` / ``attached_images`` / ``edit_snapshots`` crash
    # the run as soon as the agent reaches the first tool call.
    session_record: dict[str, Any] = {
        "agent": None,
        "agent_error": None,
        "agent_ready": threading.Event(),
        "attached_images": [],
        "cols": int(params.get("cols", 80) or 80),
        "cwd": cwd,
        "edit_snapshots": {},
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "image_counter": 0,
        "pending_title": None,
        "model_override": None,
        "create_reasoning_override": None,
        "create_service_tier_override": None,
        "close_on_disconnect": False,
        "profile_context": profile_context,
        "agent_profile_id": agent_profile_id,
        "agentProfileId": agent_profile_id,
        "agent_context_mode": None,
        "activity_event_bus": activity_event_bus,
        "runtime_scope_key": runtime_scope_key,
        "run_context": run_context,
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
        "session_key": frame.stored_session_id,
        "show_reasoning": False,
        "slash_worker": None,
        "tool_progress_mode": None,
        "tool_started_at": {},
        "transport": _server._stdio_transport,  # the NoopTransport
        "transient": transient,
        "workspace": workspace,
    }
    # Hydrate conversation history from the canonical control_home DB
    # so the agent's ``run_conversation(conversation_history=...)`` call
    # — fed from this ``session_record["history"]`` — sees recent prior
    # turns on this stable session. Without this every worker turn
    # starts from an empty history: the agent has no memory of earlier
    # messages, can't answer "what did I just ask?", and a cancelled
    # turn's partial assistant text never returns to context even
    # though it lives in the messages table.
    #
    # Architectural note: per-turn ``_ensure_worker_session`` creates a
    # fresh ``runtime_sid`` so two turns of the same stored_session never
    # share an in-memory session_record. The DB hydration here is what
    # bridges them — control_home/state.db is the single source of
    # truth (Phase 8b), so the read sees every persist + every
    # ``persist_interrupted_partial`` from prior turns including
    # cross-restart history. Failures here degrade gracefully to
    # empty history rather than crashing the turn.
    #
    # Bounded to the most recent ``_HYDRATE_TAIL_LIMIT`` messages: a
    # long-running session can accumulate 100+ messages (assistant
    # turns + each tool call + each tool result + each reasoning
    # block), and every API call would re-ship the whole tail through
    # the LLM context, hammering latency and cost. The agent's
    # session_search tool covers older history on demand. Trim
    # respects role boundaries — never start the slice on an orphan
    # ``tool`` message whose ``assistant(tool_calls=...)`` parent was
    # left behind, because the provider rejects that as a malformed
    # sequence and the agent's ``repair_message_sequence`` would drop
    # the tool result anyway.
    try:
        db = _server._db_for_stable_session(frame.stored_session_id)
    except Exception:
        db = None
    history_reader = None
    if db is not None:
        history_reader = getattr(db, "get_conversation_message_read_model", None)
        if not callable(history_reader):
            history_reader = getattr(db, "get_messages_as_conversation", None)
    if callable(history_reader):
        try:
            full_history = list(history_reader(frame.stored_session_id))
            if _should_project_member_perspective(run_context):
                try:
                    participants = db.list_conversation_participants(  # type: ignore[attr-defined]
                        frame.stored_session_id
                    )
                except Exception:
                    participants = []
                from hermes_team_mission.state.session_views import (
                    transform_to_member_perspective,
                )

                full_history = transform_to_member_perspective(
                    full_history,
                    viewing_participant_id=run_context.participant_id,
                    participants=participants,
                )
        except Exception:
            _log.warning(
                "[agent-runner] history hydration failed stored_session=%s",
                frame.stored_session_id, exc_info=True,
            )
            full_history = []
        trimmed_history = _trim_history_to_window(full_history)
        session_record["history"] = trimmed_history

    with _server._sessions_lock:
        _server._sessions[runtime_sid] = session_record

    return runtime_sid, session_record


_HYDRATE_TAIL_LIMIT = 40


def _is_team_member_identity_contract_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    try:
        from hermes_team_mission.state.session_views import (
            is_team_member_identity_contract_message,
        )
    except Exception:
        return False
    return is_team_member_identity_contract_message(message)


def _trim_history_to_window(history: list) -> list:
    """Return the last ``_HYDRATE_TAIL_LIMIT`` messages, advanced
    forward to the next non-``tool`` message so the slice never
    starts on an orphan tool result. Returns the original list
    unchanged when it's at or under the limit."""
    if len(history) <= _HYDRATE_TAIL_LIMIT:
        return history
    pinned: list = []
    body: list = []
    for message in history:
        if _is_team_member_identity_contract_message(message):
            if not pinned:
                pinned.append(message)
            continue
        body.append(message)
    if pinned:
        body_limit = max(_HYDRATE_TAIL_LIMIT - len(pinned), 0)
        if len(body) <= body_limit:
            return pinned + body
        start = len(body) - body_limit
        while start < len(body) and (
            isinstance(body[start], dict)
            and body[start].get("role") == "tool"
        ):
            start += 1
        return pinned + body[start:]
    start = len(history) - _HYDRATE_TAIL_LIMIT
    while start < len(history) and (
        isinstance(history[start], dict)
        and history[start].get("role") == "tool"
    ):
        start += 1
    return history[start:]


def _watch_for_cancel(
    sid: str,
    session: dict,
    frame: RunStartFrame,
    cancel_event: threading.Event,
) -> None:
    """Background thread: when ``cancel_event`` is set, translate it
    into the legacy interrupt path so the agent's polling sees it.

    The legacy ``session.interrupt`` handler mutates the session's
    ``interrupted_*`` keys + bumps ``interrupt_seq`` + calls into the
    agent's own interrupt RPC. We replicate the relevant subset
    inline — calling the @method handler from here would require a
    proper transport, and we don't have one inside the worker for
    this synthetic path."""
    cancel_event.wait()
    try:
        with session["history_lock"]:
            session["interrupted_run_id"] = frame.run_id
            session["interrupted_turn_id"] = frame.turn_id
            session["interrupt_seq"] = int(session.get("interrupt_seq") or 0) + 1
        from tui_gateway.methods.session import _request_agent_interrupt_async
        _request_agent_interrupt_async(sid, session.get("agent"))
    except Exception:
        _log.exception(
            "[agent-runner] cancel propagation failed run_id=%s", frame.run_id,
        )


def run_agent(frame: RunStartFrame, cancel_event: threading.Event) -> None:
    """The ``AgentRunner`` callable wired into ``AgentRunBackend``.

    Runs on a background thread spawned by ``AgentRunBackend.start``.
    Returns only after the agent has truly stopped — so the publish
    bridge stays installed for every event the agent emits."""
    setup_worker_environment()

    from tui_gateway import server as _server
    from tui_gateway.methods.prompt import _execute_prompt_submit

    sid, session = _ensure_worker_session(frame)

    watcher = threading.Thread(
        target=_watch_for_cancel,
        args=(sid, session, frame, cancel_event),
        name=f"agent-cancel-watch[{frame.run_id}]",
        daemon=True,
    )
    watcher.start()

    base_params = frame.params if isinstance(frame.params, dict) else {}
    # The main side's ``primary_dispatch`` already stripped the keys
    # it lifted into named ``RunStartFrame`` fields; re-merge them now.
    prompt_params: dict[str, Any] = {
        **base_params,
        "session_id": sid,
        "stored_session_id": frame.stored_session_id,
        "text": frame.prompt,
        "run_id": frame.run_id,
        "client_run_id": frame.run_id,
        "turn_id": frame.turn_id,
        # The main sidecar has already done the registry reservation
        # via ``run.reserve``; bypass the legacy adapter's recursive
        # forward through run.submit.
        "_run_registry_reserved": True,
    }

    # ``rid`` is the JSON-RPC request id used by ``_ok`` / ``_err``
    # envelope builders only; the agent code never reads it.
    rid = f"worker-{frame.run_id}"
    resp = _execute_prompt_submit(rid, prompt_params)
    if isinstance(resp, dict) and resp.get("error"):
        err = resp["error"]
        raise RuntimeError(
            f"_execute_prompt_submit returned error code={err.get('code')} "
            f"message={err.get('message')!r}"
        )

    # CRITICAL: ``_execute_prompt_submit`` spawns its own background
    # thread for the agent run and returns immediately. We must NOT
    # return from ``run_agent`` until the agent stops; otherwise the
    # ``AgentRunBackend`` will uninstall ``WorkerPublishBridge`` and
    # subsequent ``publish_recorded_event`` calls — including the
    # agent's terminal ``message.complete`` — will go through the
    # restored original publish path (DB only, no stdout EventFrame).
    _block_until_run_finished(session, frame, cancel_event)


def _block_until_run_finished(
    session: dict,
    frame: RunStartFrame,
    cancel_event: threading.Event,
) -> None:
    """Poll the session's ``active_run_id`` / ``running`` flags until
    the legacy agent thread (spawned by ``_execute_prompt_submit``)
    clears them.

    Per ``methods/prompt.py``, the agent's ``finally`` block on the
    background thread clears ``active_run_id`` and sets
    ``running=False``. Polling is the lowest-coupling synchronization
    point — adding an explicit ``Event`` would require touching the
    legacy code path.
    """
    poll_interval_s = 0.05
    cancel_drain_total_s = 8.0
    cancel_drain_check_s = 0.1

    while True:
        with session["history_lock"]:
            active = str(session.get("active_run_id") or "")
            running = bool(session.get("running"))
        if active != frame.run_id and not running:
            return
        if cancel_event.is_set():
            # The watcher already pushed the interrupt; give the
            # agent a bounded grace period to drain its final emit
            # before we return and let the bridge uninstall.
            deadline = time.monotonic() + cancel_drain_total_s
            while time.monotonic() < deadline:
                with session["history_lock"]:
                    if str(session.get("active_run_id") or "") != frame.run_id:
                        return
                time.sleep(cancel_drain_check_s)
            _log.warning(
                "[agent-runner] cancel drain timed out run_id=%s",
                frame.run_id,
            )
            return
        time.sleep(poll_interval_s)
