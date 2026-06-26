# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


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
