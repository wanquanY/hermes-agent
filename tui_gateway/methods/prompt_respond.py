# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from typing import Any

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


# ── Methods: respond ─────────────────────────────────────────────────
#
# PR-5 (I8): three-state respond contract.
#
#   respond(request_id, choice) →
#     registry hit + pending   → resolve → {status:"resolved", resolved:1}
#     registry hit + resolved  → {error: 4409, "already_resolved",
#                                 resolved_choice: <previous>}
#     registry miss            → {error: 4404, "unknown_request"}
#
# Error codes 4404/4409 are dedicated to the interactive-respond contract
# and do NOT collide with the legacy 4009 (session busy / generic no-pending)
# or 4001 (session not found) used by the approval policy / session paths.
#
# The in-process ``_pending_registry`` (PendingRegistry) tracks request
# state so a second respond to the same request_id returns
# ``already_resolved`` instead of silently swallowing the answer or
# erroring with a generic code. The legacy ``server._pending`` dict
# (``{request_id: (sid, threading.Event)}``) remains the unblock
# primitive — the registry layers state tracking on top.


def _pending_registry():
    """Lazily create / fetch the process-local PendingRegistry.

    Stored on ``server`` so all method modules share one instance and so
    tests can reset it via ``server._interactive_registry.clear()``.
    """
    from tui_gateway import server

    reg = getattr(server, "_interactive_registry", None)
    if reg is None:
        from hermes_agent.orchestration.worker_frame_router import PendingRegistry

        reg = PendingRegistry(
            publish_event=_publish_interaction_event,
        )
        server._interactive_registry = reg  # type: ignore[attr-defined]
    return reg


def _publish_interaction_event(event_type: str, entry: Any) -> None:
    """Persist interaction lifecycle transitions outside the canonical stream."""
    from tui_gateway.services.interaction_registry import persist_interaction_event

    stable = str(getattr(entry, "session_key", "") or getattr(entry, "conversation_id", "") or "").strip()
    if not stable:
        raise ValueError("interaction persistence requires a stable session id")
    db = _db_for_stable_session(stable)
    saved = persist_interaction_event(db, event_type, entry)
    if not saved:
        raise RuntimeError(
            f"interaction persistence failed type={event_type} request_id={getattr(entry, 'request_id', '')}"
        )


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


def _requested_approval_session_id(params: dict) -> str:
    return str(
        params.get("conversation_session_id")
        or params.get("conversationSessionId")
        or params.get("session_id")
        or params.get("sessionId")
        or ""
    ).strip()


def _live_approval_session_key(requested: str) -> str:
    if not requested:
        return ""
    session = _sessions.get(requested)
    if session:
        return str(session.get("session_key") or requested)
    for runtime_sid, live_session in list(_sessions.items()):
        if str((live_session or {}).get("session_key") or "") == requested:
            return str((live_session or {}).get("session_key") or runtime_sid)
    return ""


def _stored_approval_session_exists(requested: str) -> bool:
    db = _db_for_stable_session(requested)
    return bool(db is not None and db.sessions.get(requested))


def resolve_approval_session_key(params: dict) -> str:
    """Resolve a stable approval session without raising to runtime routing."""
    requested = _requested_approval_session_id(params)
    live_key = _live_approval_session_key(requested)
    if live_key:
        return live_key
    try:
        return requested if requested and _stored_approval_session_exists(requested) else ""
    except Exception:
        return ""


def _respond(rid, params, key):
    """Three-state respond contract for in-process prompt/sudo/secret requests.

    Uses ``server._pending`` as the unblock primitive and the
    PendingRegistry for state tracking (pending → resolved / expired).
    """
    r = str(params.get("request_id", "") or "").strip()
    reg = _pending_registry()

    # Check the registry first for already-resolved / expired state.
    known = reg.lookup(r)
    if known is not None:
        if known.state == "resolved":
            prev = known.choice
            if prev is None:
                return _err(rid, 4409, "already_resolved")
            return _err_with_data(
                rid, 4409, "already_resolved",
                {"resolved_choice": prev},
            )
        if known.state == "expired":
            return _err(rid, 4404, "unknown_request")

    # Try to resolve via the in-process unblock dict.
    with _prompt_lock:
        entry = _pending.get(r)
        if not entry:
            # Not in the unblock dict. If the registry didn't know it
            # either, it's a true miss. If the registry knew it as
            # pending (registered but _block already popped its event —
            # rare race), treat as unknown.
            return _err(rid, 4404, "unknown_request")
        sid = entry[0] if isinstance(entry, tuple) else ""
        ev = entry[1] if isinstance(entry, tuple) else entry

    # Lazy registration: _register_inprocess_pending only wrote _pending,
    # not the registry. Register now so the TTL/expiry check fires on the
    # next lookup, and a subsequent respond returns 4409.
    if known is None:
        reg.register(
            request_id=r, kind=_key_to_kind(key), conversation_id=sid,
        )
        known = reg.lookup(r)
    # After lazy registration, the lookup may have flipped the entry to
    # expired (lazy TTL check). If so, treat as unknown — do NOT unblock.
    if known is not None and known.state == "expired":
        return _err(rid, 4404, "unknown_request")

    answer = params.get(key, "")
    if not reg.mark_resolved(r, choice=answer):
        return _err(rid, 4404, "unknown_request")
    with _prompt_lock:
        _answers[r] = answer
        ev.set()
    return _ok(rid, {"status": "resolved", "resolved": 1})


def _respond_gateway_clarify(rid, params: dict):
    """Three-state respond for gateway clarify requests.

    PR-5: the old code returned ``None`` (silent swallow) when
    ``resolve_gateway_clarify`` returned False. This made a duplicate
    respond or a respond to an expired/unknown request look like success
    to the caller (the ``clarify.respond`` @method fell through to
    ``_respond`` which then returned ``{status:"ok"}`` for a miss it
    couldn't distinguish from a hit). Now returns explicit 4404
    ``unknown_request`` so the frontend can surface the error.
    """
    r = str(params.get("request_id", "") or "").strip()
    if not r:
        return _err(rid, 4404, "unknown_request")
    reg = _pending_registry()

    # Already resolved? Return 4409 with the previous choice.
    known = reg.lookup(r)
    if known is not None and known.state == "resolved":
        prev = known.choice
        if prev is None:
            return _err(rid, 4409, "already_resolved")
        return _err_with_data(
            rid, 4409, "already_resolved",
            {"resolved_choice": prev},
        )
    # Expired → unknown
    if known is not None and known.state == "expired":
        return _err(rid, 4404, "unknown_request")

    try:
        from tools import clarify_gateway as _clarify_mod
    except Exception:
        return _err(rid, 4404, "unknown_request")
    pending_entry = None
    if hasattr(_clarify_mod, "get_pending_by_request_id"):
        try:
            pending_entry = _clarify_mod.get_pending_by_request_id(r)
        except Exception as exc:
            return _err(rid, 5004, str(exc))
    session_key = str(getattr(pending_entry, "session_key", "") or "").strip()
    try:
        resolved = _clarify_mod.resolve_gateway_clarify(r, params.get("answer", ""))
    except Exception as exc:
        return _err(rid, 5004, str(exc))
    if not resolved:
        # PR-5: no longer return None (silent swallow). The clarify was
        # not found / already resolved / expired in the clarify gateway's
        # own registry. Return explicit 4404.
        return _err(rid, 4404, "unknown_request")
    # Register + mark resolved so a subsequent respond gets 4409.
    if known is None:
        reg.register(request_id=r, kind="clarify", session_key=session_key)
    if not reg.mark_resolved(r, choice=params.get("answer", "")):
        return _err(rid, 4404, "unknown_request")
    return _ok(rid, {"status": "resolved", "resolved": 1, "source": "clarify_gateway"})


# Map the legacy ``key`` param (answer field name) to the interactive kind.
_KEY_KIND_MAP = {
    "answer": "clarify",
    "password": "sudo",
    "value": "secret",
    "choice": "approval",
}


def _key_to_kind(key: str) -> str:
    return _KEY_KIND_MAP.get(str(key or ""), str(key or ""))


# Sentinel imported lazily to avoid a hard module-level dependency on
# worker_frame_router (which is imported lazily in _pending_registry).
# We use ``None`` as "no previous choice" — a real deny/empty answer is
# a non-None value (string "deny", empty string "" is falsy but not None
# in practice; clarify answers are non-empty strings). The
# ``resolved_choice`` method on PendingRegistry returns ``_MISSING`` (a
# distinct sentinel) when the entry is not resolved, but for the respond
# contract we only check ``known.state == "resolved"`` before reading
# ``known.choice``, so None-vs-MISSING is handled by the state check.


def _err_with_data(rid, code: int, msg: str, data: dict) -> dict:
    """Like _err but includes a ``data`` field in the error object."""
    err = _err(rid, code, msg)
    err["error"]["data"] = data
    return err


def _approval_session_key(params: dict, rid):
    """Resolve policy and legacy approval requests to a stable session key."""
    requested = _requested_approval_session_id(params)
    if not requested:
        return "", _err(rid, 4006, "session_id or conversation_session_id required")

    live_key = _live_approval_session_key(requested)
    if live_key:
        return live_key, None

    try:
        if _stored_approval_session_exists(requested):
            return requested, None
    except Exception as exc:
        return "", _err(rid, 5004, str(exc))

    return "", _err(rid, 4001, "session not found")


@method("clarify.respond")
def _(rid, params: dict) -> dict:
    gateway_response = _respond_gateway_clarify(rid, params)
    if gateway_response is not None:
        return gateway_response
    # _respond_gateway_clarify now always returns a dict (never None);
    # this fallback is unreachable but kept for defensive symmetry.
    return _respond(rid, params, "answer")


@method("sudo.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "password")


@method("secret.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "value")


@method("approval.respond")
def _(rid, params: dict) -> dict:
    """Three-state approval respond.

    PR-5: supports ``request_id``-addressed resolution. When ``params``
    carries a ``request_id``, the handler looks up the blocking
    approval via ``find_gateway_approval_by_request_id`` and resolves
    that specific entry. The legacy session-key path
    (``_approval_session_key``) is retained as a fallback for clients
    that still address by ``session_id`` and is marked deprecated.

    The old ``has_blocking_approval`` pre-check is removed — it was a
    fast-path that bypassed the registry and returned a raw
    ``{resolved: N}`` payload, which is incompatible with the
    three-state contract (a 0-count resolve looked like success).
    """
    reg = _pending_registry()
    request_id = str(
        params.get("request_id")
        or params.get("requestId")
        or ""
    ).strip()
    choice = params.get("choice", "deny")
    resolve_all = params.get("all", False)
    reason = params.get("reason") if choice == "deny" else None

    # ── request_id-addressed resolution (preferred) ──────────────────
    if request_id:
        # Already resolved? Return 4409 with the previous choice.
        known = reg.lookup(request_id)
        if known is not None and known.state == "resolved":
            prev = known.choice
            if prev is None:
                return _err(rid, 4409, "already_resolved")
            return _err_with_data(
                rid, 4409, "already_resolved",
                {"resolved_choice": prev},
            )
        # Expired → unknown
        if known is not None and known.state == "expired":
            return _err(rid, 4404, "unknown_request")

        try:
            from tools.approval import (
                find_gateway_approval_by_request_id,
                resolve_gateway_approval,
            )
        except Exception as e:
            return _err(rid, 5004, str(e))

        found = find_gateway_approval_by_request_id(request_id)
        if found is None:
            # Not in the approval index. Could be a clarify/sudo/secret
            # request that the client mis-routed to approval.respond, or
            # a genuinely unknown id. Return 4404 per the contract.
            return _err(rid, 4404, "unknown_request")

        session_key = found.get("session_key") or ""
        try:
            count = resolve_gateway_approval(
                request_id,
                choice,
                resolve_all=resolve_all,
                reason=reason,
            )
        except Exception as e:
            return _err(rid, 5004, str(e))
        if count == 0:
            # Entry was in the index but the queue was empty (race with
            # a parallel resolve). Treat as unknown per the contract.
            return _err(rid, 4404, "unknown_request")
        # Register + mark resolved so a subsequent respond gets 4409.
        if known is None:
            reg.register(
                request_id=request_id, kind="approval",
                session_key=session_key,
            )
        reg.mark_resolved(request_id, choice=choice)
        return _ok(rid, {"status": "resolved", "resolved": count})

    # ── legacy session-key resolution (deprecated fallback) ──────────
    session_key, err = _approval_session_key(params, rid)
    if err:
        return err
    try:
        from tools.approval import resolve_gateway_approval

        count = resolve_gateway_approval(
            session_key,
            choice,
            resolve_all=resolve_all,
            reason=reason,
        )
    except Exception as e:
        return _err(rid, 5004, str(e))
    if count == 0:
        # PR-5: no longer return _ok({resolved:0}) — that was a false
        # success. Nothing was pending → unknown_request.
        return _err(rid, 4404, "unknown_request")
    return _ok(rid, {"status": "resolved", "resolved": count})


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
