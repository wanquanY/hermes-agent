# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any

from agent.dovie_diagnostics import emit_dovie_diagnostic
from hermes_runtime_event_payloads import terminal_text_metadata
from hermes_team_mission.state.conversation import normalize_team_mission_conversation_session
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services import run_control
from tui_gateway.services.prompt_attachments import submitted_attachments as _normalize_submitted_attachments
from tui_gateway.services.prompt_attachments import submitted_image_paths as _normalize_submitted_image_paths
from tui_gateway.services.prompt_image_routing import build_image_aware_run_message
from tui_gateway.services.runtime_credentials import ensure_agent_runtime_current
from tui_gateway.services.toolset_scope import ensure_session_turn_toolsets
from tui_gateway.services.voice import voice_tts_enabled

_server = bind_server_globals(globals())


# ── Methods: prompt ──────────────────────────────────────────────────


def _attachment_path_helpers():
    from tui_gateway.services.attachment_paths import (
        IMAGE_EXTENSIONS,
        detect_file_drop,
        resolve_attachment_path,
        split_path_input,
    )

    cli_mod = sys.modules.get("cli")
    if cli_mod is not None:
        return (
            getattr(cli_mod, "_IMAGE_EXTENSIONS", IMAGE_EXTENSIONS),
            getattr(cli_mod, "_detect_file_drop", detect_file_drop),
            getattr(cli_mod, "_resolve_attachment_path", resolve_attachment_path),
            getattr(cli_mod, "_split_path_input", split_path_input),
        )

    return (
        IMAGE_EXTENSIONS,
        detect_file_drop,
        resolve_attachment_path,
        split_path_input,
    )


def _log_prompt_stage(session: dict, sid: str, stage: str, **fields: Any) -> None:
    run_id = str(session.get("active_run_id") or fields.pop("run_id", "") or "")
    turn_id = str(session.get("active_turn_id") or fields.pop("turn_id", "") or "")
    runtime_scope_key = str(
        session.get("active_runtime_scope_key")
        or session.get("runtime_scope_key")
        or session.get("session_key")
        or sid
    )
    if not run_id and not runtime_scope_key.startswith("team:"):
        return
    pairs = {
        "stage": stage,
        "sid": sid,
        "conversation_session_id": str(session.get("session_key") or sid),
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        **fields,
    }
    emit_dovie_diagnostic("[dovie-prompt-stage]", pairs)


def _text_probe(value: Any) -> dict[str, Any]:
    text = str(value or "")
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return {
        "len": len(text),
        "sha1": digest,
        "preview": text[:80].replace("\n", "\\n"),
    }


def _payload_text(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("delta", "text", "snapshot", "output", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _apply_dovie_product_runtime_policy(agent: Any, raw_context: Any) -> None:
    if agent is None or not isinstance(raw_context, dict):
        return
    team_mission = raw_context.get("team_mission") or raw_context.get("teamMission")
    team_mission = team_mission if isinstance(team_mission, dict) else {}
    setattr(
        agent,
        "_delegate_inherits_parent_tools",
        bool(team_mission.get("delegate_inherits_parent_tools") or team_mission.get("delegateInheritsParentTools")),
    )


def _prompt_terminal_status_from_result(result: dict, raw: Any) -> str:
    if result.get("interrupted"):
        return "interrupted"
    error = str(result.get("error") or "").strip()
    if not error:
        return "complete"
    raw_text = str(raw or "").strip()
    if not raw_text:
        return "error"
    if bool(result.get("failed")) and raw_text.lower().startswith(("error:", "failed:", "exception:")):
        return "error"
    return "complete"


def _worker_bootstrap_model_is_preselected(
    session: dict,
    requested_model: str,
    _model_descriptor: dict,
) -> bool:
    """Return whether the control plane already selected this worker model.

    ``RunStartFrame`` materialization stores the turn's explicit model in the
    worker session before ``prompt.submit`` runs.  Replaying that selection
    through the interactive ``/model`` pipeline is both redundant and wrong:
    it performs synchronous provider discovery before the agent even exists.
    The override is materialized from the same immutable RunStartFrame as the
    prompt request, so an exact model match is the authority boundary.  A
    descriptor enriches reasoning/context semantics when present, but must not
    be required: internal team and restored-session runs can legitimately omit
    it, and routing those runs through interactive ``/model`` validation makes
    first-token delivery depend on a relay exposing a reachable ``/models``.
    """
    from tui_gateway.process_role import is_worker_process

    if not is_worker_process() or session.get("agent") is not None:
        return False
    override = session.get("model_override")
    if not isinstance(override, dict):
        return False
    override_model = str(override.get("model") or "").strip()
    return bool(
        requested_model
        and override_model == requested_model
        and bool(override.get("model_explicit"))
    )


def _apply_prompt_model_selection(
    sid: str,
    session: dict,
    requested_model: str,
    model_descriptor: dict,
) -> None:
    """Apply a prompt model without re-validating worker bootstrap state."""
    if _worker_bootstrap_model_is_preselected(
        session,
        requested_model,
        model_descriptor,
    ):
        # The deferred agent build consumes session["model_override"].  Bind
        # the descriptor now so bind_session_agent() replays reasoning/context
        # semantics before the first provider request.
        _set_session_model_descriptor(session, model_descriptor, clear_if_empty=True)
        return

    # Interactive/direct TUI callers still use the canonical model-switch
    # pipeline.  Only commit the descriptor after a successful switch so a
    # rejected model cannot mutate the currently-running agent's semantics.
    catalog_model_id = _authoritative_catalog_model_id(model_descriptor)
    switch_options = {
        # A prompt-selected model belongs to this conversation.  Keep the
        # terminal CLI's global persistence policy out of the RPC contract.
        "parsed_flags": (requested_model, "", False, False, True),
    }
    if catalog_model_id:
        switch_options["catalog_model_id"] = catalog_model_id
    _apply_model_switch(
        sid,
        session,
        requested_model,
        **switch_options,
    )
    _set_session_model_descriptor(session, model_descriptor, clear_if_empty=True)


def _mark_prompt_run_failed(
    *,
    run_id: str,
    conversation_session_id: str,
    runtime_scope_key: str,
    turn_id: str = "",
    message: str = "",
) -> None:
    from tui_gateway.process_role import is_worker_process

    # Worker processes are event sources, never terminal-state writers.  The
    # AgentRunBackend converts the raised prompt failure into RunTerminalFrame;
    # WorkerFrameRouter applies that frame in the main process and publishes
    # the live terminal event after the DB transition commits.
    if is_worker_process():
        return
    db = _db_for_stable_session(conversation_session_id)
    if db is None or not run_id or not conversation_session_id:
        return
    run_control.terminate_run(
        conversation_session_id=conversation_session_id,
        run_id=run_id,
        turn_id=turn_id,
        runtime_scope_key=runtime_scope_key or conversation_session_id,
        status="failed",
        message=message,
        db=db,
        owner_transport=current_transport(),
    )


def _mark_prompt_run_cancelled(
    *,
    run_id: str,
    conversation_session_id: str,
    runtime_scope_key: str,
    turn_id: str = "",
    message: str = "",
) -> dict:
    from tui_gateway.process_role import is_worker_process

    event = None
    if not is_worker_process():
        db = _db_for_stable_session(conversation_session_id)
        if db is not None and run_id and conversation_session_id:
            event = run_control.terminate_run(
                conversation_session_id=conversation_session_id,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=runtime_scope_key or conversation_session_id,
                status="cancelled",
                message=message or "cancelled before prompt start",
                db=db,
                owner_transport=current_transport(),
            )
    return {
        "status": "cancelled",
        "run_id": run_id,
        "turn_id": turn_id,
        "conversation_session_id": conversation_session_id,
        "runtime_scope_key": runtime_scope_key or conversation_session_id,
        "seq": int((event or {}).get("seq") or 0),
    }


def _fail_unavailable_runtime_agent(
    *,
    sid: str,
    session: dict,
    run_id: str,
    turn_id: str,
    message: str = "runtime agent unavailable",
) -> None:
    _emit("error", sid, {"message": message})
    with session["history_lock"]:
        session["running"] = False
        session["active_run_id"] = None
        session["active_turn_id"] = None
        session["pending_turn"] = None
    if not session.get("transient"):
        _mark_prompt_run_failed(
            run_id=run_id,
            conversation_session_id=str(session.get("session_key") or sid),
            runtime_scope_key=str(
                session.get("runtime_scope_key")
                or session.get("active_runtime_scope_key")
                or session.get("session_key")
                or sid
            ),
            turn_id=turn_id,
            message=message,
        )


def _persist_prompt_user_turn(
    *,
    sid: str,
    session: dict,
    conversation_session_id: str,
    runtime_scope_key: str,
    run_id: str,
    turn_id: str,
    client_message_id: str,
    text: Any,
    persist_user_message: str,
    user_message_persistence: str,
    attachments: list[dict],
    draft_text: str,
    model: str,
    model_descriptor: dict,
    dovie_product_context: str,
) -> None:
    if session.get("transient"):
        return
    if str(user_message_persistence or "").strip().lower() == "external":
        # A control-plane writer already committed the canonical visible user
        # event for this run/turn. The execution runtime consumes the input but
        # is not a second transcript owner.
        return
    canonical_session_id = str(conversation_session_id or "").strip()
    if not canonical_session_id or not run_id or not turn_id:
        return
    content = str(persist_user_message or text or "")
    if not content.strip() and not attachments:
        return
    db = _db_for_stable_session(canonical_session_id)
    if db is None:
        _log_prompt_stage(
            session,
            sid,
            "user-persist-skipped",
            run_id=run_id,
            turn_id=turn_id,
            reason="db-unavailable",
        )
        return
    try:
        if db.sessions.get(canonical_session_id) is None:
            db.sessions.create(canonical_session_id, source="tui", transient=False)
    except Exception as exc:
        logger.warning(
            "prompt.submit user turn session ensure failed sid=%s conversation_session_id=%s: %s",
            sid,
            canonical_session_id,
            exc,
            exc_info=True,
        )
    metadata: dict[str, Any] = {
        "run_id": run_id,
        "turn_id": turn_id,
        "participant_id": "user",
        "participantId": "user",
        "turn_message_index": 0,
        "persist_message_key": f"run:{run_id}|turn:{turn_id}|idx:0",
        "runtime_scope_key": runtime_scope_key or canonical_session_id,
        "conversation_session_id": canonical_session_id,
        "prompt_submit_owned": True,
    }
    if client_message_id:
        metadata["client_message_id"] = client_message_id
    if attachments:
        metadata["attachments"] = attachments
    if draft_text:
        metadata["draft_text"] = draft_text
    if model:
        metadata["model"] = model
    if model_descriptor:
        metadata["model_descriptor"] = model_descriptor
    if dovie_product_context:
        metadata["dovie_product_context"] = dovie_product_context
    try:
        message_id = db.messages.append(
            session_id=canonical_session_id,
            role="user",
            content=content,
            participant_id="user",
            metadata=metadata,
        )
        _log_prompt_stage(
            session,
            sid,
            "user-persisted",
            run_id=run_id,
            turn_id=turn_id,
            message_id=message_id,
            content_len=len(content),
            client_message_id=client_message_id,
        )
    except Exception as exc:
        logger.warning(
            "prompt.submit user turn persistence failed sid=%s conversation_session_id=%s run_id=%s turn_id=%s: %s",
            sid,
            canonical_session_id,
            run_id,
            turn_id,
            exc,
            exc_info=True,
        )
        _log_prompt_stage(
            session,
            sid,
            "user-persist-failed",
            run_id=run_id,
            turn_id=turn_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )


class _MessageDeltaNormalizer:
    """Normalizes agent stream callbacks into explicit Gateway text events.

    The Gateway ABI is append-only for string stream callbacks.  Earlier
    versions tried to infer cumulative/snapshot callbacks from text content,
    but that is not a valid protocol: legitimate chunks can share a prefix with
    prior output (for example later markdown labels beginning with the same
    Chinese word as the response).  Any producer that needs snapshot semantics
    must send an explicit structured callback instead of a bare string.
    """

    def __init__(self) -> None:
        self.text = ""

    @staticmethod
    def _structured_value(value) -> dict:
        if not isinstance(value, dict):
            return {}
        return value

    @staticmethod
    def _protocol_offset(value: str) -> int:
        return len(str(value or "").encode("utf-16-le")) // 2

    def feed(self, value) -> dict | None:
        if value is None:
            return None
        structured = self._structured_value(value)
        mode = str(structured.get("mode") or "").strip().lower()
        if structured:
            raw_value = structured.get("delta") or structured.get("text") or structured.get("output")
        else:
            raw_value = value
        incoming = str(raw_value or "")
        if not incoming:
            return None
        if mode in {"snapshot", "replace", "cumulative"}:
            return self.feed_snapshot(incoming)
        current = self.text
        offset = self._protocol_offset(self.text)
        self.text = current + incoming
        return {
            "mode": "append",
            "text": incoming,
            "delta": incoming,
            "offset": offset,
        }

    def feed_snapshot(self, value: str) -> dict | None:
        snapshot = str(value or "")
        if not snapshot:
            return None
        if snapshot == self.text:
            return None
        if not snapshot.startswith(self.text):
            return None
        delta = snapshot[len(self.text):]
        if not delta:
            return None
        offset = self._protocol_offset(self.text)
        self.text = snapshot
        return {
            "mode": "append",
            "text": delta,
            "delta": delta,
            "offset": offset,
        }

    def reconcile_final_text(self, value: str) -> dict | None:
        return self.feed_snapshot(str(value or ""))

    def reset(self) -> None:
        self.text = ""


@method("prompt.submit")
def _(rid, params: dict) -> dict:
    if not params.get("_run_registry_reserved"):
        target = str(
            params.get("conversation_session_id")
            or params.get("conversationSessionId")
            or params.get("session_id")
            or ""
        ).strip()
        sid, session = _resolve_runtime_session(target)
        conversation_session_id = str((session or {}).get("session_key") or target).strip()
        if not conversation_session_id:
            return _err(rid, 4006, "conversation_session_id or session_id required")
        return _methods["run.submit"](
            rid,
            {
                **params,
                "conversation_session_id": conversation_session_id,
                "session_id": conversation_session_id,
                "_legacy_prompt_adapter": True,
                **({"execution_session_id": sid} if sid else {}),
            },
        )
    return _execute_prompt_submit(rid, params)


def _execute_prompt_submit(rid, params: dict) -> dict:
    sid, text = params.get("session_id", ""), params.get("text", "")
    requested_model = str(params.get("model") or "").strip()
    model_descriptor = _normalize_model_descriptor(params.get("model_descriptor") or params.get("modelDescriptor"))
    has_model_descriptor = bool(params.get("model_descriptor") or params.get("modelDescriptor"))
    run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    turn_id = str(params.get("turn_id") or uuid.uuid4().hex).strip()
    runtime_scope_key = str(params.get("runtime_scope_key") or params.get("runtimeScopeKey") or "").strip()
    client_message_id = str(params.get("client_message_id") or "").strip()
    current_input_conversation_message_id = str(
        params.get("current_input_conversation_message_id")
        or params.get("currentInputConversationMessageId")
        or ""
    ).strip()
    current_input_identity = (
        {"current_input_conversation_message_id": current_input_conversation_message_id}
        if current_input_conversation_message_id
        else {}
    )
    persist_user_message = str(
        params.get("persist_user_message")
        or params.get("persistUserMessage")
        or params.get("transcript_text")
        or params.get("transcriptText")
        or ""
    )
    user_message_persistence = str(
        params.get("user_message_persistence")
        or params.get("userMessagePersistence")
        or "runtime"
    ).strip().lower()
    if user_message_persistence not in {"runtime", "external"}:
        return _err(rid, 4002, "user_message_persistence must be 'runtime' or 'external'")
    turn_system_context = str(
        params.get("turn_system_context")
        or params.get("turnSystemContext")
        or ""
    ).strip()
    raw_dovie_context = params.get("dovie_product_context") or params.get("dovieProductContext") or ""
    dovie_product_context = (
        json.dumps(raw_dovie_context, ensure_ascii=False)
        if isinstance(raw_dovie_context, (dict, list))
        else str(raw_dovie_context or "").strip()
    )
    submitted_attachments = _submitted_attachments(params)
    submitted_images = _submitted_image_paths({"attachments": submitted_attachments})
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    conversation_session_id = str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or session.get("session_key")
        or sid
    ).strip()
    effective_runtime_scope_key = runtime_scope_key or conversation_session_id
    approval_policy = str(
        params.get("approval_policy")
        or params.get("approvalPolicy")
        or params.get("permission_mode")
        or params.get("permissionMode")
        or ""
    ).strip().lower()
    if approval_policy:
        if approval_policy not in {"default", "full_access"}:
            return _err(rid, 4002, f"unknown approval policy mode: {approval_policy}")
        try:
            from tools.approval import disable_session_yolo, enable_session_yolo

            if approval_policy == "full_access":
                enable_session_yolo(conversation_session_id)
            else:
                disable_session_yolo(conversation_session_id)
        except Exception as e:
            return _err(rid, 5004, str(e))
    with session["history_lock"]:
        preinterrupted_run_id = str(session.get("interrupted_run_id") or "")
        preinterrupted_turn_id = str(session.get("interrupted_turn_id") or "")
        if (
            (preinterrupted_run_id and preinterrupted_run_id == run_id)
            or (preinterrupted_turn_id and preinterrupted_turn_id == turn_id)
        ):
            session["running"] = False
            session["active_run_id"] = None
            session["active_turn_id"] = None
            session["pending_turn"] = None
            session["run_updated_at"] = time.time()
            return _ok(
                rid,
                _mark_prompt_run_cancelled(
                    run_id=run_id,
                    conversation_session_id=conversation_session_id,
                    runtime_scope_key=effective_runtime_scope_key,
                    turn_id=turn_id,
                    message="cancelled before prompt start",
                ),
            )
    with session["history_lock"]:
        if session.get("running"):
            if not session.get("transient"):
                _mark_prompt_run_failed(
                    run_id=run_id,
                    conversation_session_id=conversation_session_id,
                    runtime_scope_key=effective_runtime_scope_key,
                    turn_id=turn_id,
                    message="session busy",
                )
            return _err(rid, 4009, "session busy")
        session["running"] = True
        session["active_run_id"] = run_id
        session["active_turn_id"] = turn_id
        session["active_runtime_scope_key"] = effective_runtime_scope_key
        session["runtime_scope_key"] = effective_runtime_scope_key
        session["pending_turn"] = {
            "turn_id": turn_id,
            "run_id": run_id,
            "client_message_id": client_message_id,
            **current_input_identity,
            "text": text,
            "attachments": submitted_attachments,
            "draft_text": str(params.get("draft_text") or text or ""),
            "persist_user_message": persist_user_message,
            "user_message_persistence": user_message_persistence,
            "turn_system_context": turn_system_context,
            "model": requested_model,
            "model_descriptor": model_descriptor,
            "dovie_product_context": dovie_product_context,
        }
        session["run_started_at"] = time.time()
        session["run_updated_at"] = session["run_started_at"]
        session["interrupted_run_id"] = ""
        session["interrupted_turn_id"] = ""
        if is_truthy_value(os.environ.get("HERMES_INTERRUPT_TRACE")):
            print(
                "[hermes] [tui_gateway] [interrupt-trace] prompt.submit.run_state "
                f"sid={sid} run_id={run_id or '-'} turn_id={turn_id or '-'} cleared_interrupt=1",
                file=sys.stderr,
                flush=True,
            )
        if not session.get("transient"):
            db = _db_for_stable_session(conversation_session_id)
            if db is not None:
                try:
                    normalize_team_mission_conversation_session(
                        db,
                        session_id=conversation_session_id,
                        metadata={"dovie_product_context": dovie_product_context},
                    )
                except Exception as exc:
                    logger.warning(
                        "team mission conversation session normalization skipped sid=%s: %s",
                        conversation_session_id,
                        exc,
                    )
            run_control.mark_run_started(
                conversation_session_id=conversation_session_id,
                execution_session_id=sid,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=effective_runtime_scope_key,
                metadata={
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                },
                db=db,
            )
            _persist_prompt_user_turn(
                sid=sid,
                session=session,
                conversation_session_id=conversation_session_id,
                runtime_scope_key=effective_runtime_scope_key,
                run_id=run_id,
                turn_id=turn_id,
                client_message_id=client_message_id,
                text=text,
                persist_user_message=persist_user_message,
                user_message_persistence=user_message_persistence,
                attachments=submitted_attachments,
                draft_text=str(params.get("draft_text") or text or ""),
                model=requested_model,
                model_descriptor=model_descriptor,
                dovie_product_context=dovie_product_context,
            )

    if requested_model:
        try:
            _apply_prompt_model_selection(
                sid,
                session,
                requested_model,
                model_descriptor,
            )
        except Exception as e:
            with session["history_lock"]:
                session["running"] = False
                session["active_run_id"] = None
                session["active_turn_id"] = None
                session["pending_turn"] = None
            if not session.get("transient"):
                _mark_prompt_run_failed(
                    run_id=run_id,
                    conversation_session_id=conversation_session_id,
                    runtime_scope_key=effective_runtime_scope_key,
                    turn_id=turn_id,
                    message=f"model switch failed: {e}",
                )
            return _err(rid, 5001, f"model switch failed: {e}")
    elif has_model_descriptor:
        _set_session_model_descriptor(session, model_descriptor, clear_if_empty=True)

    ensure_session_turn_toolsets(
        sid=sid,
        session=session,
        requested_toolsets=params.get("enabled_toolsets") or params.get("enabledToolsets"),
        load_enabled_toolsets=_load_enabled_toolsets,
        emit_session_info=lambda event_sid, agent: _emit(
            "session.info",
            event_sid,
            _session_info(agent, _sessions.get(event_sid)),
        ),
        requested_disabled_toolsets=params.get("disabled_toolsets") or params.get("disabledToolsets"),
        load_disabled_toolsets=_load_disabled_toolsets,
        toolset_scope=(
            params.get("toolset_scope")
            or params.get("toolsetScope")
            or params.get("toolset_mode")
            or params.get("toolsetMode")
        ),
        persist_session_id=None if session.get("transient") else conversation_session_id,
    )
    _start_agent_build(sid, session)

    def run_after_agent_ready() -> None:
        profile_tokens = _enter_profile_context(session.get("profile_context"))
        try:
            err = _wait_agent(session, rid)
            if err:
                _emit(
                    "error",
                    sid,
                    {
                        "message": err.get("error", {}).get(
                            "message", "agent initialization failed"
                        )
                    },
                )
                with session["history_lock"]:
                    session["running"] = False
                    session["active_run_id"] = None
                    session["active_turn_id"] = None
                    session["pending_turn"] = None
                _mark_prompt_run_failed(
                    run_id=run_id,
                    conversation_session_id=conversation_session_id,
                    runtime_scope_key=effective_runtime_scope_key,
                    turn_id=turn_id,
                    message=err.get("error", {}).get("message", "agent initialization failed"),
                )
                return
            if session.get("agent") is None:
                _fail_unavailable_runtime_agent(
                    sid=sid,
                    session=session,
                    run_id=run_id,
                    turn_id=turn_id,
                )
                return
            try:
                ensure_agent_runtime_current(
                    sid=sid,
                    session=session,
                    resolve_model=_resolve_model,
                    emit_session_info=lambda event_sid, agent: _emit(
                        "session.info",
                        event_sid,
                        _session_info(agent, _sessions.get(event_sid)),
                    ),
                )
            except Exception as e:
                _emit("error", sid, {"message": f"runtime auth rebind failed: {e}"})
                with session["history_lock"]:
                    session["running"] = False
                    session["active_run_id"] = None
                    session["active_turn_id"] = None
                    session["pending_turn"] = None
                _mark_prompt_run_failed(
                    run_id=run_id,
                    conversation_session_id=conversation_session_id,
                    runtime_scope_key=effective_runtime_scope_key,
                    turn_id=turn_id,
                    message=f"runtime auth rebind failed: {e}",
                )
                return
            _apply_dovie_product_runtime_policy(session.get("agent"), raw_dovie_context)
            with session["history_lock"]:
                if (
                    str(session.get("interrupted_run_id") or "") == run_id
                    or str(session.get("interrupted_turn_id") or "") == turn_id
                    or str(session.get("active_run_id") or "") != run_id
                    or turn_id in set(session.get("recalled_turn_ids") or set())
                ):
                    return
            turn_metadata = {
                "turn_id": turn_id,
                "run_id": run_id,
                "client_message_id": client_message_id,
                **current_input_identity,
                "attachments": submitted_attachments,
                "draft_text": str(params.get("draft_text") or text or ""),
                "persist_user_message": persist_user_message,
                "user_message_persistence": user_message_persistence,
                "turn_system_context": turn_system_context,
                "model": requested_model,
                "model_descriptor": model_descriptor,
                "reasoning_config": _turn_reasoning_config(params),
                "dovie_product_context": dovie_product_context,
            }
        finally:
            _leave_profile_context(profile_tokens)
        _run_prompt_submit(rid, sid, session, text, submitted_images, turn_metadata)

    threading.Thread(target=run_after_agent_ready, daemon=True).start()
    return _ok(
        rid,
        {
            "status": "streaming",
            "run_id": run_id,
            "turn_id": turn_id,
            "client_message_id": client_message_id,
            "session_id": sid,
            "conversation_session_id": conversation_session_id,
            "runtime_scope_key": effective_runtime_scope_key,
        },
    )


def _submitted_attachments(params: dict) -> list[dict]:
    return _normalize_submitted_attachments(params)


def _submitted_image_paths(params: dict) -> list[str]:
    return _normalize_submitted_image_paths(params)


def _turn_reasoning_config(params: dict) -> dict | None:
    raw = params.get("reasoning_config")
    if raw is None:
        raw = params.get("reasoningConfig")
    if not isinstance(raw, dict):
        return None
    config = dict(raw)
    if config.get("enabled") is False:
        return {"enabled": False}
    effort = str(config.get("effort") or "").strip()
    if effort:
        return {"effort": effort}
    return config or None


def _turn_identity(metadata: dict | None) -> dict:
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        key: str(metadata.get(key) or "").strip()
        for key in ("run_id", "turn_id", "client_message_id")
        if str(metadata.get(key) or "").strip()
    }


def _turn_matches(candidate: dict, target: dict) -> bool:
    if not candidate or not target:
        return False
    return any(
        candidate.get(key) and target.get(key) and candidate.get(key) == target.get(key)
        for key in ("run_id", "turn_id", "client_message_id")
    )


def _latest_assistant_message_id_for_turn(session_id: str, turn_metadata: dict | None) -> str:
    target = _turn_identity(turn_metadata)
    if not session_id or not target:
        return ""
    db = _db_for_stable_session(session_id)
    if db is None:
        return ""
    try:
        messages = db.messages.all_as_conversation(
            session_id,
            include_ancestors=False,
            include_storage_metadata=True,
        )
    except Exception:
        return ""

    active_turn: dict = {}
    latest_message_id = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if role == "user":
            active_turn = _turn_identity(metadata)
            continue
        if role != "assistant":
            continue
        message_turn = _turn_identity(metadata) or active_turn
        if _turn_matches(message_turn, target):
            latest_message_id = str(message.get("message_id") or "").strip()
    return latest_message_id


def _commit_scope_summary_after_compression(
    *,
    session: dict,
    history: list[dict],
) -> None:
    run_context = session.get("run_context")
    if run_context is None:
        return
    conversation_session_id = str(
        getattr(run_context, "conversation_session_id", "") or ""
    ).strip()
    snapshot_id = str(
        getattr(run_context, "activity_context_snapshot_id", "")
        or getattr(run_context, "context_snapshot_id", "")
        or ""
    ).strip()
    if not conversation_session_id or not snapshot_id:
        return
    try:
        db = _db_for_stable_session(conversation_session_id)
        memory_service = getattr(db, "conversation_memory", None) if db is not None else None
        if memory_service is None:
            return
        from hermes_agent.domain.context_compaction import (
            ContextCompactionService,
            ContextScope,
        )

        ContextCompactionService(memory_service).checkpoint(
            ContextScope.from_run_context(run_context),
            history,
        )
    except RuntimeError as exc:
        logger.info(
            "context summary CAS lost conversation=%s participant=%s: %s",
            conversation_session_id,
            str(getattr(run_context, "participant_id", "") or ""),
            exc,
        )
    except Exception:
        logger.warning(
            "context summary commit failed conversation=%s participant=%s",
            conversation_session_id,
            str(getattr(run_context, "participant_id", "") or ""),
            exc_info=True,
        )


def _run_prompt_submit(
    rid,
    sid: str,
    session: dict,
    text: Any,
    submitted_images: list[str] | None = None,
    turn_metadata: dict | None = None,
) -> None:
    with session["history_lock"]:
        history = list(session["history"])
        history_version = int(session.get("history_version", 0))
        images = [*list(session.get("attached_images", [])), *list(submitted_images or [])]
        turn_run_id = str(session.get("active_run_id") or "")
        turn_id = str((turn_metadata or {}).get("turn_id") or session.get("active_turn_id") or "")
        turn_client_message_id = str(
            (turn_metadata or {}).get("client_message_id") or ""
        ).strip()
        session["attached_images"] = []
    agent = session.get("agent")
    if agent is None:
        _log_prompt_stage(session, sid, "agent-missing", run_id=turn_run_id, turn_id=turn_id)
        _fail_unavailable_runtime_agent(
            sid=sid,
            session=session,
            run_id=turn_run_id,
            turn_id=turn_id,
        )
        return
    user_message_persistence = str(
        (turn_metadata or {}).get("user_message_persistence") or "runtime"
    ).strip().lower()
    turn_system_context = str(
        (turn_metadata or {}).get("turn_system_context") or ""
    ).strip()
    compression_count_before = int(
        getattr(getattr(agent, "context_compressor", None), "compression_count", 0)
        or 0
    )
    delta_normalizer = _MessageDeltaNormalizer()
    message_segment_index = 0
    reasoning_text_by_message_seq: dict[str, str] = {}

    def current_client_message_id() -> str:
        base = str(turn_id or turn_run_id or sid or "prompt-turn").strip()
        return f"{base}:assistant-segment:{message_segment_index}"

    def current_message_seq_in_run() -> int:
        return message_segment_index + 1

    def current_message_identity_payload() -> dict[str, Any]:
        message_seq = current_message_seq_in_run()
        client_message_id = current_client_message_id()
        return {
            "client_message_id": client_message_id,
            "clientMessageId": client_message_id,
            "message_seq_in_run": message_seq,
            "messageSeqInRun": message_seq,
        }

    def _emit_reasoning_delta(reasoning_text: str) -> None:
        if is_turn_interrupted():
            return
        incoming = str(reasoning_text or "")
        if not incoming:
            return
        identity = current_message_identity_payload()
        message_seq = str(identity.get("message_seq_in_run") or "")
        current_reasoning = reasoning_text_by_message_seq.get(message_seq, "")
        next_reasoning = current_reasoning + incoming
        reasoning_text_by_message_seq[message_seq] = next_reasoning
        payload: dict[str, Any] = {
            "source": "provider_reasoning",
            "mode": "append",
            "text": incoming,
            "delta": incoming,
            "offset": _MessageDeltaNormalizer._protocol_offset(current_reasoning),
            "snapshot": next_reasoning,
            **identity,
        }
        _emit("reasoning.delta", sid, payload)

    _log_prompt_stage(
        session,
        sid,
        "before-message-start",
        run_id=turn_run_id,
        turn_id=turn_id,
        history_count=len(history),
        image_count=len(images),
        text_len=len(str(text or "")),
        message_seq_in_run=current_message_seq_in_run(),
    )
    _emit("message.start", sid, current_message_identity_payload())
    _log_prompt_stage(session, sid, "after-message-start", run_id=turn_run_id, turn_id=turn_id)

    def terminalize_if_still_active(reason: str) -> None:
        from tui_gateway.process_role import is_worker_process

        # Worker events are asynchronous stdout frames. The worker cannot read
        # main-process DB state as an acknowledgement that its terminal frame
        # has been applied; doing so races the pipe and previously attempted an
        # illegal runs.terminate DB RPC. RunTerminalFrame is the main-side
        # reconciliation barrier for worker execution.
        if is_worker_process():
            return
        if session.get("transient") or not turn_run_id:
            return
        conversation_session_id = str(session.get("session_key") or sid)
        db = _db_for_stable_session(conversation_session_id)
        if db is None:
            return
        try:
            state = db.runs.get(turn_run_id) or {}
        except Exception as exc:
            logger.warning(
                "[dovie-prompt] terminal fallback state lookup failed sid=%s run_id=%s error=%s",
                sid,
                turn_run_id,
                exc,
            )
            return
        status = str(state.get("status") or "").strip()
        if status not in run_control.ACTIVE_RUN_STATUSES:
            return
        # If the user (or main-side ``run.cancel``) interrupted this turn,
        # the legacy interrupt path marked ``session["interrupted_run_id"]``
        # to ``turn_run_id``. Surface the terminal as ``cancelled`` so the
        # UI shows "已中断" instead of "运行失败" — the in-flight stream
        # didn't fail; it was deliberately stopped.
        with session["history_lock"]:
            interrupted_run_id = str(session.get("interrupted_run_id") or "")
        was_cancelled = bool(turn_run_id and interrupted_run_id == turn_run_id)
        fallback_status = "cancelled" if was_cancelled else "failed"
        logger.warning(
            "[dovie-prompt] terminal fallback for active run sid=%s conversation_session_id=%s "
            "run_id=%s turn_id=%s status=%s fallback=%s reason=%s",
            sid,
            conversation_session_id,
            turn_run_id,
            turn_id,
            status,
            fallback_status,
            reason,
        )
        run_control.terminate_run(
            conversation_session_id=conversation_session_id,
            run_id=turn_run_id,
            turn_id=turn_id,
            runtime_scope_key=str(
                session.get("active_runtime_scope_key")
                or session.get("runtime_scope_key")
                or session.get("session_key")
                or sid
            ),
            execution_session_id=sid,
            status=fallback_status,
            message=reason,
            db=db,
            owner_transport=current_transport(),
        )

    def is_turn_interrupted() -> bool:
        with session["history_lock"]:
            interrupted_run_id = str(session.get("interrupted_run_id") or "")
            interrupted_turn_id = str(session.get("interrupted_turn_id") or "")
            active_run_id = str(session.get("active_run_id") or "")
            if turn_run_id and interrupted_run_id == turn_run_id:
                if is_truthy_value(os.environ.get("HERMES_INTERRUPT_TRACE")):
                    print(
                        "[hermes] [tui_gateway] [interrupt-trace] prompt.stream.skip_interrupted_run "
                        f"sid={sid} run_id={turn_run_id or '-'} turn_id={turn_id or '-'}",
                        file=sys.stderr,
                        flush=True,
                    )
                return True
            if turn_id and interrupted_turn_id == turn_id:
                if is_truthy_value(os.environ.get("HERMES_INTERRUPT_TRACE")):
                    print(
                        "[hermes] [tui_gateway] [interrupt-trace] prompt.stream.skip_interrupted_turn "
                        f"sid={sid} run_id={turn_run_id or '-'} turn_id={turn_id or '-'}",
                        file=sys.stderr,
                        flush=True,
                    )
                return True
            if turn_id and turn_id in set(session.get("recalled_turn_ids") or set()):
                return True
            stale = bool(turn_run_id and active_run_id and active_run_id != turn_run_id)
            if stale:
                if is_truthy_value(os.environ.get("HERMES_INTERRUPT_TRACE")):
                    print(
                        "[hermes] [tui_gateway] [interrupt-trace] prompt.stream.skip_stale_active_run "
                        f"sid={sid} run_id={turn_run_id or '-'} active_run_id={active_run_id or '-'} turn_id={turn_id or '-'}",
                        file=sys.stderr,
                        flush=True,
                    )
            return stale

    def close_current_text_segment(reason: str = "stream_boundary") -> None:
        nonlocal message_segment_index
        current_text = str(delta_normalizer.text or "")
        if not current_text:
            delta_normalizer.reset()
            return
        _log_prompt_stage(
            session,
            sid,
            "stream-text-segment-closed",
            run_id=turn_run_id,
            turn_id=turn_id,
            reason=reason,
            segment_index=message_segment_index,
            accumulated_text_len=len(current_text),
        )
        message_segment_index += 1
        delta_normalizer.reset()

    def persist_interrupted_partial(base_messages: list[dict] | None = None) -> None:
        # Captures the in-flight stream segment's accumulated text so an
        # interrupt can still anchor a partial assistant reply against the
        # user turn that triggered it. ``delta_normalizer.text`` is reset
        # to '' every time a text segment completes (see _emit_text_
        # message_complete around line 805), so for a turn that already
        # streamed a clean text segment and then moved on to a tool call
        # before the user cancelled, ``partial`` is empty even though
        # there IS real assistant content to preserve — that content is
        # in ``base_messages`` (the agent's full run_conversation
        # return). The earlier ``if not partial: return`` short-circuit
        # threw away the user's turn AND the streamed assistant text in
        # that case, so the next turn loaded session messages that
        # didn't include the cancelled turn at all — agent answered
        # "what was my last message?" with the message BEFORE the
        # cancelled one. Bug repro pattern:
        #   1. user submits message
        #   2. agent streams a text segment ("好的, 我来搜索...")
        #   3. agent moves to a tool call (delta_normalizer.reset())
        #   4. user terminates
        #   5. next turn: agent has no record of (1)+(2)
        partial = delta_normalizer.text.strip()

        next_history = [
            dict(message)
            for message in (base_messages or [])
            if isinstance(message, dict)
        ]
        if not next_history:
            next_history = list(history)

        # Nothing meaningful to write — neither a partial mid-segment
        # text nor a passed-in base_messages snapshot exceeds the
        # session's existing history. Skip without disturbing the
        # session's history_version.
        if not partial and len(next_history) <= len(history):
            return

        current_turn_id = str((turn_metadata or {}).get("turn_id") or "")
        current_run_id = str((turn_metadata or {}).get("run_id") or "")

        def is_current_user_message(message: dict) -> bool:
            if message.get("role") != "user":
                return False
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if current_turn_id and str(metadata.get("turn_id") or "") == current_turn_id:
                return True
            if current_run_id and str(metadata.get("run_id") or "") == current_run_id:
                return True
            return message.get("content") == text

        if not any(is_current_user_message(message) for message in next_history[len(history):]):
            user_message = {"role": "user", "content": text}
            if turn_metadata:
                user_message["metadata"] = turn_metadata
            next_history.append(user_message)

        # Append the still-streaming partial assistant text only when
        # there IS one. base_messages may already carry a finalized
        # assistant message for the same content — skip the dup append
        # in that case.
        if partial:
            # Mark this row as interrupted on TWO sides:
            #   - ``finish_reason="interrupted"`` reaches the messages
            #     table via append_message and surfaces to providers
            #     that look at the previous assistant's finish_reason
            #     when deciding whether the turn is still in flight.
            #   - ``metadata.interrupted = True`` is the canonical
            #     in-band marker the agent's own history-validation
            #     paths check (see ``repair_message_sequence`` /
            #     resume heuristics).
            # Together they prevent the next turn's agent from treating
            # the truncated row as a still-pending continuation and
            # hammering memory_search to "find the rest" — the symptom
            # reported as "after cancel, next message hangs in memory
            # retrieval loops".
            assistant_message = {
                "role": "assistant",
                "content": partial,
                "finish_reason": "interrupted",
            }
            interrupt_metadata: dict[str, Any] = {"interrupted": True}
            if turn_metadata:
                interrupt_metadata["turn_id"] = turn_metadata.get("turn_id")
                interrupt_metadata["run_id"] = turn_metadata.get("run_id")
            assistant_message["metadata"] = interrupt_metadata
            current_turn_start = max(
                (
                    index
                    for index, message in enumerate(next_history)
                    if isinstance(message, dict) and is_current_user_message(message)
                ),
                default=-1,
            )
            last_tool_boundary = max(
                (
                    index
                    for index, message in enumerate(next_history)
                    if index > current_turn_start
                    and isinstance(message, dict)
                    and (
                        message.get("role") == "tool"
                        or bool(message.get("tool_calls"))
                    )
                ),
                default=current_turn_start,
            )
            existing_partial = next(
                (
                    message
                    for message in reversed(next_history[last_tool_boundary + 1:])
                    if isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and not message.get("tool_calls")
                    and str(message.get("content") or "").strip() == partial
                ),
                None,
            )
            if existing_partial is not None:
                existing_partial.setdefault("metadata", {})
                if isinstance(existing_partial["metadata"], dict):
                    existing_partial["metadata"].update(interrupt_metadata)
                existing_partial["finish_reason"] = "interrupted"
            else:
                next_history.append(assistant_message)
        with session["history_lock"]:
            if int(session.get("history_version", 0)) != history_version:
                return
            session["history"] = next_history
            session["history_version"] = history_version + 1
        if hasattr(agent, "_persist_session"):
            if session.get("transient"):
                return
            try:
                agent._persist_session(next_history, conversation_history=history)
            except Exception as exc:
                print(
                    f"[tui_gateway] interrupted partial persistence failed sid={sid}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def run():
        worker_started_at = time.time()
        approval_token = None
        session_tokens = []
        profile_tokens = []
        goal_followup = None  # set by the post-turn goal hook below
        post_turn_history = list(history)
        terminal_attempted = False
        try:
            _log_prompt_stage(
                session,
                sid,
                "worker-entry",
                run_id=turn_run_id,
                turn_id=turn_id,
            )
            _log_prompt_stage(session, sid, "profile-context-enter-start", run_id=turn_run_id, turn_id=turn_id)
            profile_tokens = _enter_profile_context(
                session.get("profile_context"),
                apply_env=False,
            )
            _log_prompt_stage(session, sid, "profile-context-enter-end", run_id=turn_run_id, turn_id=turn_id)
            from tools.approval import (
                reset_current_session_key,
                set_current_session_key,
            )

            _log_prompt_stage(session, sid, "session-context-enter-start", run_id=turn_run_id, turn_id=turn_id)
            approval_token = set_current_session_key(session["session_key"])
            session_cwd = _session_cwd(session)
            session_tokens = _set_session_context(
                session["session_key"],
                terminal_cwd=session_cwd,
                dovie_product_context=str((turn_metadata or {}).get("dovie_product_context") or ""),
            )
            _log_prompt_stage(
                session,
                sid,
                "session-context-enter-end",
                run_id=turn_run_id,
                turn_id=turn_id,
                cwd=session_cwd,
            )
            cols = session.get("cols", 80)
            streamer = make_stream_renderer(cols)
            prompt = text
            clean_prompt = (
                None
                if user_message_persistence == "external"
                else str((turn_metadata or {}).get("persist_user_message") or prompt or "")
            )

            if isinstance(prompt, str) and "@" in prompt:
                _log_prompt_stage(session, sid, "context-reference-preprocess-start", run_id=turn_run_id, turn_id=turn_id)
                from agent.context_references import preprocess_context_references
                from agent.model_metadata import get_model_context_length

                ctx_len = get_model_context_length(
                    getattr(agent, "model", "") or _resolve_model(),
                    base_url=getattr(agent, "base_url", "") or "",
                    api_key=getattr(agent, "api_key", "") or "",
                    provider=getattr(agent, "provider", "") or "",
                    config_context_length=getattr(
                        agent, "_config_context_length", None
                    ),
                    allow_network_discovery=False,
                )
                ctx = preprocess_context_references(
                    prompt,
                    cwd=session_cwd,
                    allowed_root=session_cwd,
                    context_length=ctx_len,
                )
                if ctx.blocked:
                    _emit(
                        "error",
                        sid,
                        {
                            "message": "\n".join(ctx.warnings)
                            or "Context injection refused."
                        },
                    )
                    return
                prompt = ctx.message
                if (
                    user_message_persistence != "external"
                    and not str((turn_metadata or {}).get("persist_user_message") or "").strip()
                ):
                    clean_prompt = prompt
                _log_prompt_stage(
                    session,
                    sid,
                    "context-reference-preprocess-end",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    prompt_len=len(str(prompt or "")),
                )

            try:
                _log_prompt_stage(session, sid, "attachment-enrichment-start", run_id=turn_run_id, turn_id=turn_id)
                from dovie_extension.prompt_attachments import enrich_prompt_with_document_attachments

                prompt = enrich_prompt_with_document_attachments(
                    prompt,
                    (turn_metadata or {}).get("attachments"),
                )
                _log_prompt_stage(
                    session,
                    sid,
                    "attachment-enrichment-end",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    prompt_len=len(str(prompt or "")),
                )
            except Exception as exc:
                print(
                    f"[tui_gateway] document attachment prompt enrichment failed: {exc}",
                    file=sys.stderr,
                )
                _log_prompt_stage(
                    session,
                    sid,
                    "attachment-enrichment-error",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    error=str(exc),
                )

            run_message = build_image_aware_run_message(
                prompt=prompt,
                prompt_text=text,
                submitted_images=images,
                session=session,
                sid=sid,
                run_id=turn_run_id,
                turn_id=turn_id,
                log_prompt_stage=_log_prompt_stage,
            )

            _log_prompt_stage(
                session,
                sid,
                "before-agent-run",
                run_id=turn_run_id,
                turn_id=turn_id,
                run_message_type=type(run_message).__name__,
                elapsed_ms=int((time.time() - worker_started_at) * 1000),
            )
            emit_dovie_diagnostic(
                "[dovie-prompt]",
                {
                    "stage": "agent-run-start",
                    "sid": sid,
                    "conversation_session_id": session.get("session_key") or sid,
                    "run_id": turn_run_id,
                    "turn_id": turn_id,
                    "runtime_scope_key": session.get("runtime_scope_key") or "",
                },
            )
            stream_delta_emitted = False

            def _stream(delta):
                nonlocal stream_delta_emitted, message_segment_index
                if is_turn_interrupted():
                    return
                if delta is None:
                    close_current_text_segment("stream_callback_none")
                    return
                input_probe = _text_probe(delta)
                payload = delta_normalizer.feed(delta)
                output_probe = _text_probe(_payload_text(payload))
                _log_prompt_stage(
                    session,
                    sid,
                    "stream-callback-normalized",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    input_len=input_probe["len"],
                    input_sha1=input_probe["sha1"],
                    input_preview=input_probe["preview"],
                    emitted=payload is not None,
                    output_mode=str((payload or {}).get("mode") or ""),
                    output_offset=(payload or {}).get("offset"),
                    output_len=output_probe["len"],
                    output_sha1=output_probe["sha1"],
                    output_preview=output_probe["preview"],
                    accumulated_text_len=len(str(delta_normalizer.text or "")),
                )
                if payload is None:
                    return
                payload.update(current_message_identity_payload())
                render_delta = payload.get("delta") or payload.get("text") or ""
                if streamer and (r := streamer.feed(render_delta)) is not None:
                    payload["rendered"] = r
                _emit("message.delta", sid, payload)
                stream_delta_emitted = True

            active_context_missing = object()
            previous_active_run_id = getattr(agent, "_hermes_active_run_id", active_context_missing)
            previous_active_turn_id = getattr(agent, "_hermes_active_turn_id", active_context_missing)
            previous_active_client_message_id = getattr(
                agent,
                "_hermes_active_client_message_id",
                active_context_missing,
            )
            previous_active_runtime_scope_key = getattr(agent, "_hermes_active_runtime_scope_key", active_context_missing)
            previous_activity_event_bus = getattr(agent, "activity_event_bus", active_context_missing)
            previous_run_context = getattr(agent, "run_context", active_context_missing)
            previous_private_run_context = getattr(agent, "_run_context", active_context_missing)
            previous_reasoning_config = getattr(agent, "reasoning_config", active_context_missing)
            previous_reasoning_callback = getattr(agent, "reasoning_callback", active_context_missing)
            previous_ephemeral_system_prompt = getattr(
                agent,
                "ephemeral_system_prompt",
                active_context_missing,
            )
            turn_reasoning_config = (
                (turn_metadata or {}).get("reasoning_config")
                if isinstance((turn_metadata or {}).get("reasoning_config"), dict)
                else None
            )
            try:
                previous_stream_text_boundary_callback = session.get(
                    "stream_text_boundary_callback",
                    active_context_missing,
                )
                session["stream_text_boundary_callback"] = close_current_text_segment
                previous_inject_tool_breaks = getattr(agent, "_stream_inject_tool_breaks", True)
                agent._stream_inject_tool_breaks = False
                agent._hermes_active_run_id = turn_run_id
                agent._hermes_active_turn_id = turn_id
                agent._hermes_active_client_message_id = turn_client_message_id
                agent._hermes_active_runtime_scope_key = str(session.get("runtime_scope_key") or "")
                if session.get("activity_event_bus") is not None:
                    agent.activity_event_bus = session.get("activity_event_bus")
                if session.get("run_context") is not None:
                    agent.run_context = session.get("run_context")
                    agent._run_context = session.get("run_context")
                if turn_reasoning_config is not None:
                    agent.reasoning_config = dict(turn_reasoning_config)
                if turn_system_context:
                    base_system_context = (
                        ""
                        if previous_ephemeral_system_prompt is active_context_missing
                        else str(previous_ephemeral_system_prompt or "").strip()
                    )
                    agent.ephemeral_system_prompt = "\n\n".join(
                        part for part in (base_system_context, turn_system_context) if part
                    )
                agent.reasoning_callback = _emit_reasoning_delta
                _log_prompt_stage(
                    session,
                    sid,
                    "agent-run-call-start",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                )
                result = agent.run_conversation(
                    run_message,
                    conversation_history=list(history),
                    stream_callback=_stream,
                    persist_user_message=clean_prompt,
                    turn_metadata=turn_metadata,
                    current_input_conversation_message_id=str(
                        (turn_metadata or {}).get(
                            "current_input_conversation_message_id"
                        )
                        or ""
                    ),
                )
                _log_prompt_stage(
                    session,
                    sid,
                    "agent-run-call-end",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    result_type=type(result).__name__,
                )
                emit_dovie_diagnostic(
                    "[dovie-prompt]",
                    {
                        "stage": "agent-run-returned",
                        "sid": sid,
                        "conversation_session_id": session.get("session_key") or sid,
                        "run_id": turn_run_id,
                        "turn_id": turn_id,
                        "result_type": type(result).__name__,
                    },
                )
            except TypeError as exc:
                if not any(
                    key in str(exc)
                    for key in (
                        "turn_metadata",
                        "persist_user_message",
                        "current_input_conversation_message_id",
                    )
                ):
                    raise
                _log_prompt_stage(
                    session,
                    sid,
                    "agent-run-compat-fallback-start",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    error=str(exc),
                )
                result = agent.run_conversation(
                    run_message,
                    conversation_history=list(history),
                    stream_callback=_stream,
                )
                _log_prompt_stage(
                    session,
                    sid,
                    "agent-run-compat-fallback-end",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    result_type=type(result).__name__,
                )
                emit_dovie_diagnostic(
                    "[dovie-prompt]",
                    {
                        "stage": "agent-run-returned-compat",
                        "sid": sid,
                        "conversation_session_id": session.get("session_key") or sid,
                        "run_id": turn_run_id,
                        "turn_id": turn_id,
                        "result_type": type(result).__name__,
                    },
                )
            finally:
                if "previous_stream_text_boundary_callback" in locals():
                    if previous_stream_text_boundary_callback is active_context_missing:
                        session.pop("stream_text_boundary_callback", None)
                    else:
                        session["stream_text_boundary_callback"] = previous_stream_text_boundary_callback
                if "previous_inject_tool_breaks" in locals():
                    agent._stream_inject_tool_breaks = previous_inject_tool_breaks
                if previous_active_run_id is active_context_missing:
                    try:
                        delattr(agent, "_hermes_active_run_id")
                    except AttributeError:
                        pass
                else:
                    agent._hermes_active_run_id = previous_active_run_id
                if previous_active_turn_id is active_context_missing:
                    try:
                        delattr(agent, "_hermes_active_turn_id")
                    except AttributeError:
                        pass
                else:
                    agent._hermes_active_turn_id = previous_active_turn_id
                if previous_active_client_message_id is active_context_missing:
                    try:
                        delattr(agent, "_hermes_active_client_message_id")
                    except AttributeError:
                        pass
                else:
                    agent._hermes_active_client_message_id = (
                        previous_active_client_message_id
                    )
                if previous_active_runtime_scope_key is active_context_missing:
                    try:
                        delattr(agent, "_hermes_active_runtime_scope_key")
                    except AttributeError:
                        pass
                else:
                    agent._hermes_active_runtime_scope_key = previous_active_runtime_scope_key
                if previous_activity_event_bus is active_context_missing:
                    try:
                        delattr(agent, "activity_event_bus")
                    except AttributeError:
                        pass
                else:
                    agent.activity_event_bus = previous_activity_event_bus
                if previous_run_context is active_context_missing:
                    try:
                        delattr(agent, "run_context")
                    except AttributeError:
                        pass
                else:
                    agent.run_context = previous_run_context
                if previous_private_run_context is active_context_missing:
                    try:
                        delattr(agent, "_run_context")
                    except AttributeError:
                        pass
                else:
                    agent._run_context = previous_private_run_context
                if previous_reasoning_config is active_context_missing:
                    try:
                        delattr(agent, "reasoning_config")
                    except AttributeError:
                        pass
                else:
                    agent.reasoning_config = previous_reasoning_config
                if previous_reasoning_callback is active_context_missing:
                    try:
                        delattr(agent, "reasoning_callback")
                    except AttributeError:
                        pass
                else:
                    agent.reasoning_callback = previous_reasoning_callback
                if previous_ephemeral_system_prompt is active_context_missing:
                    try:
                        delattr(agent, "ephemeral_system_prompt")
                    except AttributeError:
                        pass
                else:
                    agent.ephemeral_system_prompt = previous_ephemeral_system_prompt

            if is_turn_interrupted():
                result_messages = (
                    result.get("messages")
                    if isinstance(result, dict) and isinstance(result.get("messages"), list)
                    else None
                )
                persist_interrupted_partial(result_messages)
                # Emit a real ``message.complete`` for the cancelled turn
                # so (a) the frontend sees a terminal status of
                # ``cancelled`` immediately (not "失败" via the
                # ``terminalize_if_still_active`` fallback), and (b) the
                # partial assistant text persists into the messages table
                # via record_event's normal reduction path — without
                # this, the next turn loads the conversation with NO
                # assistant message for the cancelled run and the agent
                # answers as if the cancelled question was never asked.
                partial_text = str(delta_normalizer.text or "")
                interrupt_payload: dict[str, Any] = {
                    "usage": _get_usage(agent),
                    "status": "cancelled",
                    "streamed": True,
                    "text": partial_text,
                    # ``interrupted`` flags this row as a terminal-by-cancel
                    # so the next turn's hydrate + provider-side message-
                    # validation see a closed assistant turn, not a
                    # half-streamed pending one (matches the
                    # ``finish_reason="interrupted"`` + metadata flag
                    # written into the messages table by
                    # ``persist_interrupted_partial``).
                    "interrupted": True,
                    **terminal_text_metadata(partial_text, prefix="text"),
                    **current_message_identity_payload(),
                }
                interrupt_message_id = _latest_assistant_message_id_for_turn(
                    str(session.get("session_key") or sid),
                    turn_metadata,
                )
                if interrupt_message_id:
                    interrupt_payload["message_id"] = interrupt_message_id
                _emit("message.complete", sid, interrupt_payload)
                terminal_attempted = True
                return

            last_reasoning = None
            status_note = None
            if isinstance(result, dict):
                compression_count_after = int(
                    getattr(
                        getattr(agent, "context_compressor", None),
                        "compression_count",
                        0,
                    )
                    or 0
                )
                if compression_count_after > compression_count_before:
                    _commit_scope_summary_after_compression(
                        session=session,
                        history=[item for item in history if isinstance(item, dict)],
                    )
                if isinstance(result.get("messages"), list):
                    with session["history_lock"]:
                        current_version = int(session.get("history_version", 0))
                        if current_version == history_version:
                            session["history"] = result["messages"]
                            session["history_version"] = history_version + 1
                            post_turn_history = list(session["history"])
                        else:
                            # History mutated externally during the turn
                            # (undo/compress/retry/rollback now guard on
                            # session.running, but this is the defensive
                            # backstop for any path that slips past).
                            # Surface the desync rather than silently
                            # dropping the agent's output — the UI can
                            # show the response and warn that it was
                            # not persisted.
                            print(
                                f"[tui_gateway] prompt.submit: history_version mismatch "
                                f"(expected={history_version} current={current_version}) — "
                                f"agent output NOT written to session history",
                                file=sys.stderr,
                            )
                            status_note = (
                                "History changed during this turn — the response above is visible "
                                "but was not saved to session history."
                            )

                # If auto-compression fired inside run_conversation(), agent.session_id
                # may have rotated. Sync session_key before downstream title/goal/finalize
                # handling uses it. Preserve pending_title (user intent) so it can be
                # applied to the continuation. Restart slash worker so subsequent
                # worker-backed commands (/title etc.) target the live session.
                # Fix for #20001.
                _sync_session_key_after_compress(
                    sid, session, clear_pending_title=False, restart_slash_worker=True,
                )

                raw = result.get("final_response", "")
                status = _prompt_terminal_status_from_result(result, raw)
                # When the backend produced no visible response AND reported a
                # real error (e.g. invalid model slug → provider 4xx), surface
                # that error as the visible text instead of shipping an empty
                # turn to Ink. Mirrors classic CLI behavior at cli.py where
                # (failed|partial) + no final_response → "Error: <detail>".
                # Leaves the None-with-no-error path untouched: an empty
                # successful turn still renders as empty, and the existing
                # "(empty)" sentinel handling stays in its own lane.
                if (not raw) and result.get("error") and (
                    result.get("failed") or result.get("partial")
                ):
                    raw = f"Error: {result.get('error')}"
                lr = result.get("last_reasoning")
                if isinstance(lr, str) and lr.strip():
                    last_reasoning = lr.strip()
            else:
                raw = str(result)
                status = "complete"

            interrupt_detail = ""
            if (
                status == "interrupted"
                and isinstance(raw, str)
                and raw.strip().startswith("Operation interrupted:")
            ):
                interrupt_detail = raw.strip()
                raw = ""
            raw_text = str(raw or "")
            final_delta_mismatch = False
            if raw_text:
                current_stream_text = str(delta_normalizer.text or "")
                should_emit_final_delta = not current_stream_text or (
                    raw_text.startswith(current_stream_text)
                    and len(raw_text) > len(current_stream_text)
                )
                raw_probe = _text_probe(raw_text)
                stream_probe = _text_probe(current_stream_text)
                _log_prompt_stage(
                    session,
                    sid,
                    "final-response-reconciliation",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    raw_len=raw_probe["len"],
                    raw_sha1=raw_probe["sha1"],
                    raw_preview=raw_probe["preview"],
                    streamed_len=stream_probe["len"],
                    streamed_sha1=stream_probe["sha1"],
                    streamed_preview=stream_probe["preview"],
                    should_emit_final_delta=should_emit_final_delta,
                    raw_startswith_stream=current_stream_text
                    and raw_text.startswith(current_stream_text),
                    stream_startswith_raw=current_stream_text.startswith(raw_text),
                )
                if should_emit_final_delta:
                    final_delta_payload = delta_normalizer.reconcile_final_text(raw_text)
                    if final_delta_payload is not None:
                        render_delta = (
                            final_delta_payload.get("delta")
                            or final_delta_payload.get("text")
                            or ""
                        )
                        if streamer and (r := streamer.feed(render_delta)) is not None:
                            final_delta_payload["rendered"] = r
                        final_delta_payload["source"] = "final_response_reconciliation"
                        final_delta_payload.update(current_message_identity_payload())
                        _emit("message.delta", sid, final_delta_payload)
                        stream_delta_emitted = True
                elif current_stream_text and raw_text != current_stream_text:
                    final_delta_mismatch = True

            payload = {
                "usage": _get_usage(agent),
                "status": status,
                "streamed": stream_delta_emitted,
                "text": raw_text,
                **current_message_identity_payload(),
                **terminal_text_metadata(raw_text, prefix="text"),
            }
            if interrupt_detail:
                payload["interrupt_detail"] = interrupt_detail
            if final_delta_mismatch:
                payload["final_text_mismatch"] = True
            if last_reasoning:
                payload.update(terminal_text_metadata(last_reasoning, prefix="reasoning"))
            if (
                isinstance(result, dict)
                and status == "complete"
                and str(result.get("error") or "").strip()
            ):
                payload["nonfatal_error"] = str(result.get("error") or "").strip()
            if status_note:
                payload["warning"] = status_note
            message_id = _latest_assistant_message_id_for_turn(
                str(session.get("session_key") or sid),
                turn_metadata,
            )
            if message_id:
                payload["message_id"] = message_id
            emit_dovie_diagnostic(
                "[dovie-prompt]",
                {
                    "stage": "message-complete-emit",
                    "sid": sid,
                    "conversation_session_id": session.get("session_key") or sid,
                    "run_id": turn_run_id,
                    "turn_id": turn_id,
                    "status": status,
                    "text_len": len(raw) if isinstance(raw, str) else 0,
                },
            )
            _emit("message.complete", sid, payload)
            terminal_attempted = True

            # ── /goal continuation (Ralph-style loop) ─────────────────
            # After every TUI turn, if a /goal is active, ask the judge
            # whether the goal is done and — if not and we're still under
            # budget — queue a continuation prompt to run after this
            # thread releases session["running"]. The verdict message
            # ("✓ Goal achieved" / "⏸ budget exhausted") is surfaced as
            # a system line so the user sees progress regardless of
            # outcome. Mirrors gateway/run._post_turn_goal_continuation.
            if status == "complete" and isinstance(raw, str) and raw.strip():
                try:
                    from hermes_cli.goals import GoalManager

                    sid_key = session.get("session_key") or ""
                    if sid_key:
                        try:
                            goals_cfg = _load_cfg().get("goals") or {}
                            goal_max_turns = int(goals_cfg.get("max_turns", 20) or 20)
                        except Exception:
                            goal_max_turns = 20
                        goal_mgr = GoalManager(
                            session_id=sid_key,
                            default_max_turns=goal_max_turns,
                        )
                        if goal_mgr.is_active():
                            try:
                                from hermes_cli.goals import gather_background_processes

                                background_processes = gather_background_processes()
                            except Exception:
                                background_processes = None
                            decision = goal_mgr.evaluate_after_turn(
                                raw,
                                user_initiated=True,
                                background_processes=background_processes,
                            )
                            verdict_msg = decision.get("message") or ""
                            if verdict_msg:
                                _emit(
                                    "status.update",
                                    sid,
                                    {"kind": "goal", "text": verdict_msg},
                                )
                            if decision.get("should_continue"):
                                cont_prompt = decision.get("continuation_prompt") or ""
                                if cont_prompt:
                                    goal_followup = cont_prompt
                except Exception as _goal_exc:
                    print(
                        f"[tui_gateway] goal continuation hook failed: "
                        f"{type(_goal_exc).__name__}: {_goal_exc}",
                        file=sys.stderr,
                    )

            # Apply pending_title now that the DB row exists.
            _pending = session.get("pending_title")
            if _pending and status == "complete":
                if session.get("transient"):
                    session["pending_title"] = None
                else:
                    _pdb = _db_for_stable_session(session.get("session_key") or sid)
                    if _pdb:
                        _session_key = session.get("session_key") or sid
                        try:
                            if _pdb.sessions.set_title(_session_key, _pending):
                                session["pending_title"] = None
                        except ValueError as exc:
                            # Invalid/duplicate title — non-retryable, drop it.
                            # Auto-title will take over. Fix for #19029.
                            session["pending_title"] = None
                            logger.info(
                                "Dropping pending title for session %s: %s",
                                _session_key, exc,
                            )
                        except Exception:
                            # Transient DB failure — keep pending_title for retry.
                            pass

            # CLI parity: when voice-mode TTS is on, speak the agent reply
            # (cli.py:_voice_speak_response).  Only the final text — tool
            # calls / reasoning already stream separately and would be
            # noisy to read aloud.
            if (
                status == "complete"
                and isinstance(raw, str)
                and raw.strip()
                and voice_tts_enabled()
            ):
                try:
                    from hermes_cli.voice import speak_text

                    spoken = raw
                    threading.Thread(
                        target=speak_text, args=(spoken,), daemon=True
                    ).start()
                except ImportError:
                    logger.warning("voice TTS skipped: hermes_cli.voice unavailable")
                except Exception as e:
                    logger.warning("voice TTS dispatch failed: %s", e)
        except Exception as e:
            import traceback

            trace = traceback.format_exc()
            try:
                os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
                with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                    f.write(
                        f"\n=== turn-dispatcher exception · "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} · sid={sid} ===\n"
                    )
                    f.write(trace)
            except Exception:
                pass
            print(
                f"[gateway-turn] {type(e).__name__}: {e}", file=sys.stderr, flush=True
            )
            _emit("error", sid, {"message": str(e)})
            terminal_attempted = True
        finally:
            try:
                if approval_token is not None:
                    reset_current_session_key(approval_token)
            except Exception:
                pass
            _clear_session_context(session_tokens)
            _leave_profile_context(profile_tokens)
            with session["history_lock"]:
                if str(session.get("active_run_id") or "") == turn_run_id:
                    session["running"] = False
                    session["active_run_id"] = None
                    session["active_turn_id"] = None
                    session["pending_turn"] = None
                    session["run_updated_at"] = time.time()
            if not terminal_attempted:
                terminalize_if_still_active("prompt worker exited before terminal event was emitted")
            else:
                terminalize_if_still_active("prompt worker terminal event did not close active run")

        # Chain a goal-continuation turn if the judge said so. We do
        # this AFTER the finally releases session["running"], so the
        # nested _run_prompt_submit doesn't deadlock on the busy
        # guard. A real user prompt that races us wins because
        # prompt.submit sets running=True under the history_lock and
        # we check that guard before re-firing.
        if goal_followup:
            followup_run_id = uuid.uuid4().hex
            followup_turn_id = uuid.uuid4().hex
            conversation_session_id = str(session.get("session_key") or sid)
            followup_scope_key = str(session.get("runtime_scope_key") or conversation_session_id)
            with session["history_lock"]:
                if session.get("running"):
                    # User already sent something — their turn wins,
                    # the judge will re-run on the next turn anyway.
                    return
                session["running"] = True
                session["active_run_id"] = followup_run_id
                session["active_turn_id"] = followup_turn_id
                session["active_runtime_scope_key"] = followup_scope_key
                session["runtime_scope_key"] = followup_scope_key
                session["run_started_at"] = time.time()
                session["run_updated_at"] = session["run_started_at"]
                session["interrupted_run_id"] = ""
                session["interrupted_turn_id"] = ""
            reservation = run_control.create_run_if_session_idle(
                conversation_session_id=conversation_session_id,
                execution_session_id=sid,
                run_id=followup_run_id,
                turn_id=followup_turn_id,
                runtime_scope_key=followup_scope_key,
                db=_db_for_stable_session(conversation_session_id),
            )
            if isinstance(reservation, dict) and reservation.get("conflict"):
                with session["history_lock"]:
                    if str(session.get("active_run_id") or "") == followup_run_id:
                        session["running"] = False
                        session["active_run_id"] = None
                        session["active_turn_id"] = None
                return
            run_control.mark_run_started(
                conversation_session_id=conversation_session_id,
                execution_session_id=sid,
                run_id=followup_run_id,
                turn_id=followup_turn_id,
                runtime_scope_key=followup_scope_key,
                metadata={
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                },
                db=_db_for_stable_session(conversation_session_id),
            )
            try:
                _run_prompt_submit(
                    rid,
                    sid,
                    session,
                    goal_followup,
                    turn_metadata={
                        "turn_id": followup_turn_id,
                        "run_id": followup_run_id,
                        "draft_text": str(goal_followup or ""),
                    },
                )
            except Exception as _cont_exc:
                print(
                    f"[tui_gateway] goal continuation dispatch failed: "
                    f"{type(_cont_exc).__name__}: {_cont_exc}",
                    file=sys.stderr,
                )
                with session["history_lock"]:
                    session["running"] = False
                    session["active_run_id"] = None

    _log_prompt_stage(session, sid, "worker-dispatch", run_id=turn_run_id, turn_id=turn_id)
    threading.Thread(target=run, daemon=True).start()


@method("clipboard.paste")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        from hermes_cli.clipboard import has_clipboard_image, save_clipboard_image
    except Exception as e:
        return _err(rid, 5027, f"clipboard unavailable: {e}")

    session["image_counter"] = session.get("image_counter", 0) + 1
    img_dir = Path(_active_hermes_home()) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = (
        img_dir
        / f"clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{session['image_counter']}.png"
    )

    # Save-first: mirrors CLI keybinding path; more robust than has_image() precheck
    if not save_clipboard_image(img_path):
        session["image_counter"] = max(0, session["image_counter"] - 1)
        msg = (
            "Clipboard has image but extraction failed"
            if has_clipboard_image()
            else "No image found in clipboard"
        )
        return _ok(rid, {"attached": False, "message": msg})

    session.setdefault("attached_images", []).append(str(img_path))
    return _ok(
        rid,
        {
            "attached": True,
            "path": str(img_path),
            "count": len(session["attached_images"]),
            **_image_meta(img_path),
        },
    )


@method("image.attach")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    raw = str(params.get("path", "") or "").strip()
    if not raw:
        return _err(rid, 4015, "path required")
    try:
        (
            image_extensions,
            detect_file_drop,
            resolve_attachment_path,
            split_path_input,
        ) = _attachment_path_helpers()

        dropped = detect_file_drop(raw)
        if dropped:
            image_path = dropped["path"]
            remainder = dropped["remainder"]
        else:
            path_token, remainder = split_path_input(raw)
            image_path = resolve_attachment_path(path_token)
            if image_path is None:
                return _err(rid, 4016, f"image not found: {path_token}")
        if image_path.suffix.lower() not in image_extensions:
            return _err(rid, 4016, f"unsupported image: {image_path.name}")
        session.setdefault("attached_images", []).append(str(image_path))
        return _ok(
            rid,
            {
                "attached": True,
                "path": str(image_path),
                "count": len(session["attached_images"]),
                "remainder": remainder,
                "text": remainder or f"[User attached image: {image_path.name}]",
                **_image_meta(image_path),
            },
        )
    except Exception as e:
        return _err(rid, 5027, str(e))


@method("input.detect_drop")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    try:
        raw = str(params.get("text", "") or "")
        _, detect_file_drop, _, _ = _attachment_path_helpers()
        dropped = detect_file_drop(raw)
        if not dropped:
            return _ok(rid, {"matched": False})

        drop_path = dropped["path"]
        remainder = dropped["remainder"]
        if dropped["is_image"]:
            session.setdefault("attached_images", []).append(str(drop_path))
            text = remainder or f"[User attached image: {drop_path.name}]"
            return _ok(
                rid,
                {
                    "matched": True,
                    "is_image": True,
                    "path": str(drop_path),
                    "count": len(session["attached_images"]),
                    "text": text,
                    **_image_meta(drop_path),
                },
            )

        text = f"[User attached file: {drop_path}]" + (
            f"\n{remainder}" if remainder else ""
        )
        return _ok(
            rid,
            {
                "matched": True,
                "is_image": False,
                "path": str(drop_path),
                "name": drop_path.name,
                "text": text,
            },
        )
    except Exception as e:
        return _err(rid, 5027, str(e))


@method("prompt.background")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    text, parent = params.get("text", ""), params.get("session_id", "")
    if not text:
        return _err(rid, 4012, "text required")
    task_id = f"bg_{uuid.uuid4().hex[:6]}"

    def run():
        session_tokens = _set_session_context(task_id, terminal_cwd=session.get("cwd"))
        try:
            from run_agent import AIAgent

            result = AIAgent(
                **_background_agent_kwargs(session["agent"], task_id)
            ).run_conversation(
                user_message=text,
                task_id=task_id,
            )
            _emit(
                "background.complete",
                parent,
                {
                    "task_id": task_id,
                    "text": (
                        result.get("final_response", str(result))
                        if isinstance(result, dict)
                        else str(result)
                    ),
                },
            )
        except Exception as e:
            _emit(
                "background.complete",
                parent,
                {"task_id": task_id, "text": f"error: {e}"},
            )
        finally:
            _clear_session_context(session_tokens)

    threading.Thread(target=run, daemon=True).start()
    return _ok(rid, {"task_id": task_id})


def has_pending_prompt(request_id: str) -> bool:
    from tui_gateway.methods import prompt_respond

    return prompt_respond.has_pending_prompt(request_id)


def resolve_approval_session_key(params: dict) -> str:
    from tui_gateway.methods import prompt_respond

    return prompt_respond.resolve_approval_session_key(params)
