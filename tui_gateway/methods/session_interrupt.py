# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.methods.session import (
    _interrupt_trace,
    _request_session_interrupt_side_effects_async,
)

_server = bind_server_globals(globals())

@method("session.interrupt")
def _(rid, params: dict) -> dict:
    sid = params.get("session_id", "")
    requested_run_id = str(params.get("run_id") or params.get("runId") or "").strip()
    requested_turn_id = str(params.get("turn_id") or params.get("turnId") or "").strip()
    completion_status = str(
        params.get("completion_status") or params.get("completionStatus") or "interrupted"
    ).strip() or "interrupted"
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    interrupted_run_id = ""
    interrupted_turn_id = ""
    interrupt_seq = 0
    should_interrupt_agent = False
    should_clear_current = False
    with session["history_lock"]:
        active_run_id = str(session.get("active_run_id") or "")
        active_turn_id = str(session.get("active_turn_id") or "")
        interrupted_run_id = requested_run_id or active_run_id
        interrupted_turn_id = requested_turn_id or active_turn_id
        interrupts_current_run = bool(interrupted_run_id and active_run_id == interrupted_run_id)
        interrupts_current_turn = bool(interrupted_turn_id and active_turn_id == interrupted_turn_id)
        has_active_target = bool(active_run_id or active_turn_id)
        has_requested_target = bool(requested_run_id or requested_turn_id)
        should_clear_current = interrupts_current_run or interrupts_current_turn
        should_interrupt_agent = (
            should_clear_current
            or (has_requested_target and not has_active_target)
            or (not has_requested_target and has_active_target)
        )
        session["interrupted_run_id"] = interrupted_run_id
        session["interrupted_turn_id"] = interrupted_turn_id
        session["interrupt_seq"] = int(session.get("interrupt_seq") or 0) + 1
        interrupt_seq = int(session.get("interrupt_seq") or 0)
        if should_clear_current:
            session["running"] = False
            session["active_run_id"] = None
            session["active_turn_id"] = None
            session["run_updated_at"] = time.time()
    _interrupt_trace(
        "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.state "
        f"sid={sid} requested_run_id={requested_run_id or '-'} requested_turn_id={requested_turn_id or '-'} "
        f"active_run_id={active_run_id or '-'} active_turn_id={active_turn_id or '-'} "
        f"interrupted_run_id={interrupted_run_id or '-'} interrupted_turn_id={interrupted_turn_id or '-'} "
        f"should_clear_current={should_clear_current} should_interrupt_agent={should_interrupt_agent} seq={interrupt_seq}",
    )
    _interrupt_trace(
        f"[hermes] [tui_gateway] session.interrupt sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'} seq={interrupt_seq}",
    )
    # Release pending prompts for this session before returning. Pending
    # clarify/sudo/secret prompts are part of the interrupted interaction even
    # when the session has no active run metadata, and resolving them
    # synchronously avoids leaving worker threads blocked behind an interrupt
    # response that already reported success.
    _clear_pending(sid)
    _request_session_interrupt_side_effects_async(
        sid=sid,
        session=session,
        should_interrupt_agent=should_interrupt_agent,
        interrupted_run_id=interrupted_run_id,
        interrupted_turn_id=interrupted_turn_id,
        completion_status=completion_status,
    )
    _interrupt_trace(
        "[hermes] [tui_gateway] [interrupt-trace] session.interrupt.return "
        f"sid={sid} run_id={interrupted_run_id or '-'} turn_id={interrupted_turn_id or '-'}",
    )
    return _ok(
        rid,
        {
            "status": "interrupted",
            "run_id": interrupted_run_id,
            "turn_id": interrupted_turn_id,
        },
    )