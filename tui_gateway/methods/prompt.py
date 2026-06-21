# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from agent.doxie_diagnostics import emit_doxie_diagnostic
from hermes_runtime_event_payloads import terminal_text_metadata
from hermes_team_mission_conversation_state import normalize_team_mission_conversation_session
from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services import run_control
from tui_gateway.services.prompt_attachments import submitted_attachments as _normalize_submitted_attachments
from tui_gateway.services.prompt_attachments import submitted_image_paths as _normalize_submitted_image_paths
from tui_gateway.services.runtime_credentials import ensure_agent_runtime_current
from tui_gateway.services.toolset_scope import ensure_session_turn_toolsets
from tui_gateway.services.voice import voice_tts_enabled

_server = bind_server_globals(globals())


# ── Methods: prompt ──────────────────────────────────────────────────


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
        "stored_session_id": str(session.get("session_key") or sid),
        "run_id": run_id,
        "turn_id": turn_id,
        "runtime_scope_key": runtime_scope_key,
        **fields,
    }
    emit_doxie_diagnostic("[doxie-prompt-stage]", pairs)


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


def _apply_doxie_product_runtime_policy(agent: Any, raw_context: Any) -> None:
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


def _mark_prompt_run_failed(
    *,
    run_id: str,
    stored_session_id: str,
    runtime_scope_key: str,
    turn_id: str = "",
    message: str = "",
) -> None:
    db = _db_for_stable_session(stored_session_id)
    if db is None or not run_id or not stored_session_id:
        return
    run_control.publish_run_terminal_event(
        stored_session_id=stored_session_id,
        run_id=run_id,
        turn_id=turn_id,
        runtime_scope_key=runtime_scope_key or stored_session_id,
        status="failed",
        message=message,
        db=db,
        owner_transport=current_transport(),
    )


def _mark_prompt_run_cancelled(
    *,
    run_id: str,
    stored_session_id: str,
    runtime_scope_key: str,
    turn_id: str = "",
    message: str = "",
) -> dict:
    db = _db_for_stable_session(stored_session_id)
    event = None
    if db is not None and run_id and stored_session_id:
        event = run_control.publish_run_terminal_event(
            stored_session_id=stored_session_id,
            run_id=run_id,
            turn_id=turn_id,
            runtime_scope_key=runtime_scope_key or stored_session_id,
            status="cancelled",
            message=message or "cancelled before prompt start",
            db=db,
            owner_transport=current_transport(),
        )
    return {
        "status": "cancelled",
        "run_id": run_id,
        "turn_id": turn_id,
        "stored_session_id": stored_session_id,
        "runtime_scope_key": runtime_scope_key or stored_session_id,
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
            stored_session_id=str(session.get("session_key") or sid),
            runtime_scope_key=str(
                session.get("runtime_scope_key")
                or session.get("active_runtime_scope_key")
                or session.get("session_key")
                or sid
            ),
            turn_id=turn_id,
            message=message,
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
        self._pending_trailing_newlines = ""

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
            self.discard_pending_trailing_newlines()
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
        if self._pending_trailing_newlines:
            incoming = self._pending_trailing_newlines + incoming
            self._pending_trailing_newlines = ""
        visible = incoming.rstrip("\n")
        self._pending_trailing_newlines = incoming[len(visible):]
        incoming = visible
        if not incoming:
            return None
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
        self._pending_trailing_newlines = ""
        return {
            "mode": "append",
            "text": delta,
            "delta": delta,
            "offset": offset,
        }

    def reconcile_final_text(self, value: str) -> dict | None:
        return self.feed_snapshot(str(value or ""))

    def discard_pending_trailing_newlines(self) -> None:
        self._pending_trailing_newlines = ""

    def reset(self) -> None:
        self.text = ""
        self._pending_trailing_newlines = ""


@method("prompt.submit")
def _(rid, params: dict) -> dict:
    if not params.get("_run_registry_reserved"):
        target = str(
            params.get("stored_session_id")
            or params.get("storedSessionId")
            or params.get("session_id")
            or ""
        ).strip()
        sid, session = _resolve_runtime_session(target)
        stable_session_id = str((session or {}).get("session_key") or target).strip()
        if not stable_session_id:
            return _err(rid, 4006, "stored_session_id or session_id required")
        return _methods["run.submit"](
            rid,
            {
                **params,
                "stored_session_id": stable_session_id,
                "session_id": stable_session_id,
                "_legacy_prompt_adapter": True,
                **({"runtime_session_id": sid} if sid else {}),
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
    persist_user_message = str(
        params.get("persist_user_message")
        or params.get("persistUserMessage")
        or params.get("transcript_text")
        or params.get("transcriptText")
        or ""
    )
    raw_doxie_context = params.get("doxie_product_context") or params.get("doxieProductContext") or ""
    doxie_product_context = (
        json.dumps(raw_doxie_context, ensure_ascii=False)
        if isinstance(raw_doxie_context, (dict, list))
        else str(raw_doxie_context or "").strip()
    )
    submitted_images = _submitted_image_paths(params)
    submitted_attachments = _submitted_attachments(params)
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    stable_session_id = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or session.get("session_key")
        or sid
    ).strip()
    effective_runtime_scope_key = runtime_scope_key or stable_session_id
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
                enable_session_yolo(stable_session_id)
            else:
                disable_session_yolo(stable_session_id)
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
                    stored_session_id=stable_session_id,
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
                    stored_session_id=stable_session_id,
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
            "text": text,
            "attachments": submitted_attachments,
            "draft_text": str(params.get("draft_text") or text or ""),
            "persist_user_message": persist_user_message,
            "model": requested_model,
            "model_descriptor": model_descriptor,
            "doxie_product_context": doxie_product_context,
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
            db = _db_for_stable_session(stable_session_id)
            if db is not None:
                try:
                    normalize_team_mission_conversation_session(
                        db,
                        session_id=stable_session_id,
                        metadata={"doxie_product_context": doxie_product_context},
                    )
                except Exception as exc:
                    logger.warning(
                        "team mission conversation session normalization skipped sid=%s: %s",
                        stable_session_id,
                        exc,
                    )
            run_control.mark_run_started(
                stored_session_id=stable_session_id,
                runtime_session_id=sid,
                run_id=run_id,
                turn_id=turn_id,
                runtime_scope_key=effective_runtime_scope_key,
                metadata={
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                },
                db=db,
            )

    if requested_model:
        try:
            _apply_model_switch(sid, session, requested_model)
            _set_session_model_descriptor(
                session,
                model_descriptor,
                clear_if_empty=True,
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
                    stored_session_id=stable_session_id,
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
        persist_session_id=None if session.get("transient") else stable_session_id,
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
                    stored_session_id=stable_session_id,
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
                    stored_session_id=stable_session_id,
                    runtime_scope_key=effective_runtime_scope_key,
                    turn_id=turn_id,
                    message=f"runtime auth rebind failed: {e}",
                )
                return
            _apply_doxie_product_runtime_policy(session.get("agent"), raw_doxie_context)
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
                "attachments": submitted_attachments,
                "draft_text": str(params.get("draft_text") or text or ""),
                "persist_user_message": persist_user_message,
                "model": requested_model,
                "model_descriptor": model_descriptor,
                "reasoning_config": _turn_reasoning_config(params),
                "doxie_product_context": doxie_product_context,
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
            "stored_session_id": stable_session_id,
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
        messages = db.get_messages_as_conversation(
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
    _log_prompt_stage(
        session,
        sid,
        "before-message-start",
        run_id=turn_run_id,
        turn_id=turn_id,
        history_count=len(history),
        image_count=len(images),
        text_len=len(str(text or "")),
    )
    _emit("message.start", sid)
    _log_prompt_stage(session, sid, "after-message-start", run_id=turn_run_id, turn_id=turn_id)

    def terminalize_if_still_active(reason: str) -> None:
        if session.get("transient") or not turn_run_id:
            return
        stored_session_id = str(session.get("session_key") or sid)
        db = _db_for_stable_session(stored_session_id)
        if db is None:
            return
        try:
            get_run = getattr(db, "get_run", None)
            state = get_run(turn_run_id) if callable(get_run) else {}
        except Exception as exc:
            logger.warning(
                "[doxie-prompt] terminal fallback state lookup failed sid=%s run_id=%s error=%s",
                sid,
                turn_run_id,
                exc,
            )
            return
        status = str(state.get("status") or "").strip()
        if status not in run_control.ACTIVE_RUN_STATUSES:
            return
        logger.warning(
            "[doxie-prompt] terminal fallback for active run sid=%s stored_session_id=%s run_id=%s turn_id=%s status=%s reason=%s",
            sid,
            stored_session_id,
            turn_run_id,
            turn_id,
            status,
            reason,
        )
        run_control.publish_run_terminal_event(
            stored_session_id=stored_session_id,
            run_id=turn_run_id,
            turn_id=turn_id,
            runtime_scope_key=str(
                session.get("active_runtime_scope_key")
                or session.get("runtime_scope_key")
                or session.get("session_key")
                or sid
            ),
            runtime_session_id=sid,
            status="failed",
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

    delta_normalizer = _MessageDeltaNormalizer()
    message_segment_index = 0

    def current_client_message_id() -> str:
        base = str(turn_id or turn_run_id or sid or "prompt-turn").strip()
        return f"{base}:assistant-segment:{message_segment_index}"

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
        partial = delta_normalizer.text.strip()
        if not partial:
            return
        assistant_message = {"role": "assistant", "content": partial}
        if turn_metadata:
            assistant_message["metadata"] = {
                "turn_id": turn_metadata.get("turn_id"),
                "run_id": turn_metadata.get("run_id"),
            }
        next_history = [
            dict(message)
            for message in (base_messages or [])
            if isinstance(message, dict)
        ]
        if not next_history:
            next_history = list(history)
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

        last_message = next_history[-1] if next_history else {}
        if (
            isinstance(last_message, dict)
            and last_message.get("role") == "assistant"
            and not last_message.get("tool_calls")
            and str(last_message.get("content") or "").strip() == partial
        ):
            if assistant_message.get("metadata") and not isinstance(last_message.get("metadata"), dict):
                last_message["metadata"] = assistant_message["metadata"]
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
                doxie_product_context=str((turn_metadata or {}).get("doxie_product_context") or ""),
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
            clean_prompt = str((turn_metadata or {}).get("persist_user_message") or prompt or "")

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
                if not str((turn_metadata or {}).get("persist_user_message") or "").strip():
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
                from doxie_extension.prompt_attachments import enrich_prompt_with_document_attachments

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

            # Decide image routing per-turn based on active provider/model.
            # "native" → pass pixels to the main model as OpenAI-style content
            # parts (adapters translate for Anthropic/Gemini/Bedrock/etc.).
            # "text"   → pre-analyze with vision_analyze and prepend the text.
            # See agent/image_routing.py for the full decision table.
            run_message: Any = prompt
            if images:
                try:
                    _log_prompt_stage(
                        session,
                        sid,
                        "image-routing-decision-start",
                        run_id=turn_run_id,
                        turn_id=turn_id,
                        image_count=len(images),
                    )
                    from agent.image_routing import (
                        decide_image_input_mode,
                        build_native_content_parts,
                    )
                    from agent.auxiliary_client import (
                        _read_main_model,
                        _read_main_provider,
                    )
                    from hermes_cli.config import load_config as _tui_load_config

                    _cfg = _tui_load_config()
                    _descriptor = dict(session.get("model_descriptor") or {})
                    _supports_vision = (
                        bool(_descriptor.get("vision_enabled"))
                        if isinstance(_descriptor.get("vision_enabled"), bool)
                        else None
                    )
                    _mode = decide_image_input_mode(
                        _read_main_provider(),
                        _read_main_model(),
                        _cfg,
                        supports_vision_override=_supports_vision,
                    )
                    _log_prompt_stage(
                        session,
                        sid,
                        "image-routing-decision-end",
                        run_id=turn_run_id,
                        turn_id=turn_id,
                        mode=_mode,
                    )
                except Exception as _img_exc:
                    print(
                        f"[tui_gateway] image_routing decision failed, defaulting to text: {_img_exc}",
                        file=sys.stderr,
                    )
                    _mode = "text"
                    _log_prompt_stage(
                        session,
                        sid,
                        "image-routing-decision-error",
                        run_id=turn_run_id,
                        turn_id=turn_id,
                        error=str(_img_exc),
                    )

                if _mode == "native":
                    try:
                        _log_prompt_stage(session, sid, "native-image-build-start", run_id=turn_run_id, turn_id=turn_id)
                        _parts, _skipped = build_native_content_parts(
                            prompt,
                            images,
                        )
                        if _skipped:
                            print(
                                f"[tui_gateway] native image attachment skipped {len(_skipped)} unreadable path(s)",
                                file=sys.stderr,
                            )
                        if any(p.get("type") == "image_url" for p in _parts):
                            run_message = _parts
                        else:
                            run_message = _enrich_with_attached_images(prompt, images)
                        _log_prompt_stage(
                            session,
                            sid,
                            "native-image-build-end",
                            run_id=turn_run_id,
                            turn_id=turn_id,
                            skipped_count=len(_skipped or []),
                            part_count=len(_parts or []),
                        )
                    except Exception as _img_exc:
                        print(
                            f"[tui_gateway] native attach failed, falling back to text: {_img_exc}",
                            file=sys.stderr,
                        )
                        run_message = _enrich_with_attached_images(prompt, images)
                        _log_prompt_stage(
                            session,
                            sid,
                            "native-image-build-error",
                            run_id=turn_run_id,
                            turn_id=turn_id,
                            error=str(_img_exc),
                        )
                else:
                    _log_prompt_stage(session, sid, "text-image-enrichment-start", run_id=turn_run_id, turn_id=turn_id)
                    run_message = _enrich_with_attached_images(prompt, images)
                    _log_prompt_stage(session, sid, "text-image-enrichment-end", run_id=turn_run_id, turn_id=turn_id)

            _log_prompt_stage(
                session,
                sid,
                "before-agent-run",
                run_id=turn_run_id,
                turn_id=turn_id,
                run_message_type=type(run_message).__name__,
                elapsed_ms=int((time.time() - worker_started_at) * 1000),
            )
            emit_doxie_diagnostic(
                "[doxie-prompt]",
                {
                    "stage": "agent-run-start",
                    "sid": sid,
                    "stored_session_id": session.get("session_key") or sid,
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
                payload["client_message_id"] = current_client_message_id()
                payload["clientMessageId"] = payload["client_message_id"]
                render_delta = payload.get("delta") or payload.get("text") or ""
                if streamer and (r := streamer.feed(render_delta)) is not None:
                    payload["rendered"] = r
                _emit("message.delta", sid, payload)
                stream_delta_emitted = True

            active_context_missing = object()
            previous_active_run_id = getattr(agent, "_hermes_active_run_id", active_context_missing)
            previous_active_turn_id = getattr(agent, "_hermes_active_turn_id", active_context_missing)
            previous_active_runtime_scope_key = getattr(agent, "_hermes_active_runtime_scope_key", active_context_missing)
            previous_reasoning_config = getattr(agent, "reasoning_config", active_context_missing)
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
                agent._hermes_active_runtime_scope_key = str(session.get("runtime_scope_key") or "")
                if turn_reasoning_config is not None:
                    agent.reasoning_config = dict(turn_reasoning_config)
                _log_prompt_stage(session, sid, "agent-run-call-start", run_id=turn_run_id, turn_id=turn_id)
                result = agent.run_conversation(
                    run_message,
                    conversation_history=list(history),
                    stream_callback=_stream,
                    persist_user_message=clean_prompt,
                    turn_metadata=turn_metadata,
                )
                _log_prompt_stage(
                    session,
                    sid,
                    "agent-run-call-end",
                    run_id=turn_run_id,
                    turn_id=turn_id,
                    result_type=type(result).__name__,
                )
                emit_doxie_diagnostic(
                    "[doxie-prompt]",
                    {
                        "stage": "agent-run-returned",
                        "sid": sid,
                        "stored_session_id": session.get("session_key") or sid,
                        "run_id": turn_run_id,
                        "turn_id": turn_id,
                        "result_type": type(result).__name__,
                    },
                )
            except TypeError as exc:
                if "turn_metadata" not in str(exc) and "persist_user_message" not in str(exc):
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
                emit_doxie_diagnostic(
                    "[doxie-prompt]",
                    {
                        "stage": "agent-run-returned-compat",
                        "sid": sid,
                        "stored_session_id": session.get("session_key") or sid,
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
                if previous_active_runtime_scope_key is active_context_missing:
                    try:
                        delattr(agent, "_hermes_active_runtime_scope_key")
                    except AttributeError:
                        pass
                else:
                    agent._hermes_active_runtime_scope_key = previous_active_runtime_scope_key
                if previous_reasoning_config is active_context_missing:
                    try:
                        delattr(agent, "reasoning_config")
                    except AttributeError:
                        pass
                else:
                    agent.reasoning_config = previous_reasoning_config

            if is_turn_interrupted():
                result_messages = (
                    result.get("messages")
                    if isinstance(result, dict) and isinstance(result.get("messages"), list)
                    else None
                )
                persist_interrupted_partial(result_messages)
                return

            last_reasoning = None
            status_note = None
            if isinstance(result, dict):
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
                        final_delta_payload["client_message_id"] = current_client_message_id()
                        final_delta_payload["clientMessageId"] = final_delta_payload["client_message_id"]
                        _emit("message.delta", sid, final_delta_payload)
                        stream_delta_emitted = True
                elif current_stream_text and raw_text != current_stream_text:
                    final_delta_mismatch = True

            payload = {
                "usage": _get_usage(agent),
                "status": status,
                "streamed": stream_delta_emitted,
                "text": raw_text,
                "client_message_id": current_client_message_id(),
                "clientMessageId": current_client_message_id(),
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
            emit_doxie_diagnostic(
                "[doxie-prompt]",
                {
                    "stage": "message-complete-emit",
                    "sid": sid,
                    "stored_session_id": session.get("session_key") or sid,
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
                            decision = goal_mgr.evaluate_after_turn(
                                raw,
                                user_initiated=True,
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
                            if _pdb.set_session_title(_session_key, _pending):
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
            stable_session_id = str(session.get("session_key") or sid)
            followup_scope_key = str(session.get("runtime_scope_key") or stable_session_id)
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
                stored_session_id=stable_session_id,
                runtime_session_id=sid,
                run_id=followup_run_id,
                turn_id=followup_turn_id,
                runtime_scope_key=followup_scope_key,
                db=_db_for_stable_session(stable_session_id),
            )
            if isinstance(reservation, dict) and reservation.get("conflict"):
                with session["history_lock"]:
                    if str(session.get("active_run_id") or "") == followup_run_id:
                        session["running"] = False
                        session["active_run_id"] = None
                        session["active_turn_id"] = None
                return
            run_control.mark_run_started(
                stored_session_id=stable_session_id,
                runtime_session_id=sid,
                run_id=followup_run_id,
                turn_id=followup_turn_id,
                runtime_scope_key=followup_scope_key,
                metadata={
                    "gateway_pid": os.getpid(),
                    "gateway_instance_id": _GATEWAY_INSTANCE_ID,
                },
                db=_db_for_stable_session(stable_session_id),
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
        from cli import (
            _IMAGE_EXTENSIONS,
            _detect_file_drop,
            _resolve_attachment_path,
            _split_path_input,
        )

        dropped = _detect_file_drop(raw)
        if dropped:
            image_path = dropped["path"]
            remainder = dropped["remainder"]
        else:
            path_token, remainder = _split_path_input(raw)
            image_path = _resolve_attachment_path(path_token)
            if image_path is None:
                return _err(rid, 4016, f"image not found: {path_token}")
        if image_path.suffix.lower() not in _IMAGE_EXTENSIONS:
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
        from cli import _detect_file_drop

        raw = str(params.get("text", "") or "")
        dropped = _detect_file_drop(raw)
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


# ── Methods: respond ─────────────────────────────────────────────────


def has_pending_prompt(request_id: str) -> bool:
    """Non-destructive check: is a prompt/sudo/secret/clarify request with this id
    pending in THIS process? Used by the gateway runtime proxy to keep an interactive
    *.respond local when the request was registered here (e.g. the in-process team
    leader conversation run) rather than proxying it to a scoped worker."""
    r = str(request_id or "").strip()
    if not r:
        return False
    try:
        with _prompt_lock:
            return r in _pending
    except Exception:
        return False


def resolve_approval_session_key(params: dict) -> str:
    """Best-effort, non-raising variant of _approval_session_key for the runtime
    proxy's local-pending check. Returns "" when it cannot resolve a session key."""
    requested = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    if not requested:
        return ""
    session = _sessions.get(requested)
    if session:
        return str(session.get("session_key") or requested)
    for runtime_sid, live_session in list(_sessions.items()):
        if str((live_session or {}).get("session_key") or "") == requested:
            return str((live_session or {}).get("session_key") or runtime_sid)
    try:
        db = _db_for_stable_session(requested)
        if db is not None and db.get_session(requested):
            return requested
    except Exception:
        return ""
    return ""


def _respond(rid, params, key):
    r = params.get("request_id", "")
    with _prompt_lock:
        entry = _pending.get(r)
        if not entry:
            return _err(rid, 4009, f"no pending {key} request")
        _, ev = entry
        _answers[r] = params.get(key, "")
        ev.set()
    return _ok(rid, {"status": "ok"})


def _respond_gateway_clarify(rid, params: dict):
    r = str(params.get("request_id", "") or "").strip()
    if not r:
        return None
    try:
        from tools import clarify_gateway as _clarify_mod
    except Exception:
        return None
    try:
        resolved = _clarify_mod.resolve_gateway_clarify(r, params.get("answer", ""))
    except Exception as exc:
        return _err(rid, 5004, str(exc))
    if not resolved:
        return None
    return _ok(rid, {"status": "ok", "source": "clarify_gateway"})


def _approval_session_key(params: dict, rid):
    requested = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    if not requested:
        return "", _err(rid, 4006, "session_id or stored_session_id required")

    session = _sessions.get(requested)
    if session:
        return str(session.get("session_key") or requested), None

    for runtime_sid, live_session in list(_sessions.items()):
        if str((live_session or {}).get("session_key") or "") == requested:
            return str((live_session or {}).get("session_key") or runtime_sid), None

    db = _db_for_stable_session(requested)
    if db is not None:
        try:
            stored = db.get_session(requested)
        except AttributeError:
            stored = None
        except Exception as exc:
            return "", _err(rid, 5004, str(exc))
        if stored:
            return requested, None

    return "", _err(rid, 4001, "session not found")


@method("clarify.respond")
def _(rid, params: dict) -> dict:
    gateway_response = _respond_gateway_clarify(rid, params)
    if gateway_response is not None:
        return gateway_response
    return _respond(rid, params, "answer")


@method("sudo.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "password")


@method("secret.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "value")


@method("approval.respond")
def _(rid, params: dict) -> dict:
    # Identity-symmetry with clarify.respond: a command approval is queued in
    # _gateway_queues keyed by session_key = the run's stable session id (set via
    # set_current_session_key(session["session_key"]) when the turn starts), which is
    # exactly the stored_session_id/session_id the client echoes back here. Resolve the
    # queue DIRECTLY by that id when an approval is actually pending, BEFORE the
    # _approval_session_key existence guard — which 4001s ("session not found") for a
    # team member node because _sessions is keyed by the runtime sid (not the stable id),
    # no live session_key scan matches, and the DB fallback classifies any "team:mission-"
    # id as control-plane and queries the wrong db. clarify.respond never hits this because
    # it resolves purely by request_id. resolve_gateway_approval is a safe no-op (returns 0)
    # if nothing is queued, so when no approval is pending we fall through to the original
    # session-resolution path unchanged.
    requested = str(
        params.get("stored_session_id")
        or params.get("storedSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()
    try:
        from tools.approval import has_blocking_approval, resolve_gateway_approval

        if requested and has_blocking_approval(requested):
            return _ok(
                rid,
                {
                    "resolved": resolve_gateway_approval(
                        requested,
                        params.get("choice", "deny"),
                        resolve_all=params.get("all", False),
                    )
                },
            )
    except Exception as e:
        return _err(rid, 5004, str(e))

    session_key, err = _approval_session_key(params, rid)
    if err:
        return err
    try:
        from tools.approval import resolve_gateway_approval

        return _ok(
            rid,
            {
                "resolved": resolve_gateway_approval(
                    session_key,
                    params.get("choice", "deny"),
                    resolve_all=params.get("all", False),
                )
            },
        )
    except Exception as e:
        return _err(rid, 5004, str(e))


@method("approval.policy.get")
def _(rid, params: dict) -> dict:
    session_key, err = _approval_session_key(params, rid)
    if err:
        return err
    try:
        from tools.approval import is_session_yolo_enabled

        yolo = is_session_yolo_enabled(session_key)
        return _ok(
            rid,
            {
                "mode": "full_access" if yolo else "default",
                "yolo": yolo,
            },
        )
    except Exception as e:
        return _err(rid, 5004, str(e))


@method("approval.policy.set")
def _(rid, params: dict) -> dict:
    session_key, err = _approval_session_key(params, rid)
    if err:
        return err
    mode = str(params.get("mode") or "default").strip().lower()
    if mode not in {"default", "full_access"}:
        return _err(rid, 4002, f"unknown approval policy mode: {mode}")
    try:
        from tools.approval import disable_session_yolo, enable_session_yolo

        if mode == "full_access":
            enable_session_yolo(session_key)
            yolo = True
        else:
            disable_session_yolo(session_key)
            yolo = False
        return _ok(rid, {"mode": mode, "yolo": yolo})
    except Exception as e:
        return _err(rid, 5004, str(e))


@method("approval.pending.list")
def _(rid, params: dict) -> dict:
    session_key, err = _approval_session_key(params, rid)
    if err:
        return err
    try:
        from tools.approval import list_gateway_approvals

        return _ok(
            rid,
            {
                "approvals": list_gateway_approvals(session_key),
            },
        )
    except Exception as e:
        return _err(rid, 5004, str(e))
