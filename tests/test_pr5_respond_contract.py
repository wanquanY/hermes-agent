"""PR-5 tests: respond three-state contract + interaction.* events.

Covers (I8):

* **respond three-state contract** — pending→resolved, already_resolved
  (4409), unknown_request (4404), expired (lazy TTL).
* **PendingRegistry** — the single source of truth for interactive
  request state (register / lookup / mark_resolved / expiry / events).
* **approval.respond request_id addressing** — resolving a blocked
  command approval by ``request_id`` via
  ``find_gateway_approval_by_request_id``.
* **silent-swallow deletion** — ``_respond_gateway_clarify`` no longer
  returns ``None`` on a miss; it returns an explicit 4404.
* **interaction.* canonical events** — requested / resolved / expired
  events are published through the registry's ``publish_event`` hook.

The in-process ``@method`` handlers in ``prompt_respond`` read
``server._pending`` (the unblock dict) and the lazily-created
``server._interactive_registry`` (PendingRegistry) for state tracking.
Tests manipulate both directly to exercise every contract branch without
standing up a full websocket / worker subprocess.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch as mock_patch

import pytest

import tools.approval as approval_module
import tools.clarify_gateway as clarify_module
from hermes_agent.orchestration.worker_frame_router import (
    PendingEntry,
    PendingRegistry,
    _MISSING,
)


# =========================================================================
# Helpers — state cleanup
# =========================================================================

def _clear_approval_globals() -> None:
    with approval_module._lock:
        approval_module._gateway_queues.clear()
        approval_module._gateway_request_index.clear()
        approval_module._gateway_notify_cbs.clear()
        approval_module._pending.clear()
        approval_module._session_approved.clear()
        approval_module._session_yolo.clear()


def _clear_clarify_globals() -> None:
    with clarify_module._lock:
        clarify_module._entries.clear()
        clarify_module._session_index.clear()


def _clear_server_pending() -> None:
    """Wipe ``server._pending`` + ``server._answers`` + the interactive registry."""
    from tui_gateway import server

    with server._prompt_lock:
        server._pending.clear()
        server._answers.clear()
    reg = getattr(server, "_interactive_registry", None)
    if reg is not None:
        reg.clear()
    # Force re-creation with a fresh publish hook next time
    if hasattr(server, "_interactive_registry"):
        del server._interactive_registry


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_approval_globals()
    _clear_clarify_globals()
    _clear_server_pending()
    yield
    _clear_approval_globals()
    _clear_clarify_globals()
    _clear_server_pending()


# =========================================================================
# PendingRegistry unit tests
# =========================================================================

class TestPendingRegistryRegister:
    def test_register_creates_pending_entry(self):
        reg = PendingRegistry()
        entry = reg.register(request_id="rid-1", kind="clarify")
        assert entry.request_id == "rid-1"
        assert entry.kind == "clarify"
        assert entry.state == "pending"
        assert entry.created_at > 0

    def test_register_re_registers_after_resolve(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        reg.mark_resolved("rid-1", choice="yes")
        # Re-register — replaces the resolved entry with a fresh pending one
        entry = reg.register(request_id="rid-1", kind="clarify")
        assert entry.state == "pending"

    def test_register_empty_request_id_raises(self):
        reg = PendingRegistry()
        with pytest.raises(ValueError):
            reg.register(request_id="", kind="clarify")

    def test_register_strips_whitespace(self):
        reg = PendingRegistry()
        entry = reg.register(request_id="  rid-1  ", kind="clarify")
        assert entry.request_id == "rid-1"


class TestPendingRegistryLookup:
    def test_lookup_known_pending(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        entry = reg.lookup("rid-1")
        assert entry is not None
        assert entry.state == "pending"

    def test_lookup_unknown_returns_none(self):
        reg = PendingRegistry()
        assert reg.lookup("nope") is None

    def test_lookup_empty_returns_none(self):
        reg = PendingRegistry()
        assert reg.lookup("") is None
        assert reg.lookup(None) is None  # type: ignore[arg-type]


class TestPendingRegistryMarkResolved:
    def test_mark_resolved_pending_returns_true(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        assert reg.mark_resolved("rid-1", choice="yes") is True
        entry = reg.lookup("rid-1")
        assert entry.state == "resolved"
        assert entry.choice == "yes"
        assert entry.resolved_at > 0

    def test_mark_resolved_unknown_returns_false(self):
        reg = PendingRegistry()
        assert reg.mark_resolved("nope", choice="yes") is False

    def test_mark_resolved_already_resolved_returns_false(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        reg.mark_resolved("rid-1", choice="yes")
        assert reg.mark_resolved("rid-1", choice="no") is False
        # Choice stays as the first resolve
        entry = reg.lookup("rid-1")
        assert entry.choice == "yes"

    def test_mark_resolved_expired_returns_false(self):
        clock = [1000.0]
        reg = PendingRegistry(ttl_seconds=10, clock=lambda: clock[0])
        reg.register(request_id="rid-1", kind="clarify")
        clock[0] = 1020  # past TTL
        assert reg.mark_resolved("rid-1", choice="yes") is False
        entry = reg.lookup("rid-1")
        assert entry.state == "expired"


class TestPendingRegistryExpiry:
    def test_lazy_expiry_on_lookup(self):
        clock = [1000.0]
        reg = PendingRegistry(ttl_seconds=10, clock=lambda: clock[0])
        reg.register(request_id="rid-1", kind="clarify")
        # Within TTL
        clock[0] = 1005
        entry = reg.lookup("rid-1")
        assert entry.state == "pending"
        # Past TTL
        clock[0] = 1015
        entry = reg.lookup("rid-1")
        assert entry.state == "expired"

    def test_expired_then_mark_resolved_fails(self):
        clock = [1000.0]
        reg = PendingRegistry(ttl_seconds=10, clock=lambda: clock[0])
        reg.register(request_id="rid-1", kind="clarify")
        clock[0] = 1020
        # lookup triggers lazy expiry
        reg.lookup("rid-1")
        assert reg.mark_resolved("rid-1", choice="yes") is False

    def test_ttl_zero_disables_expiry(self):
        clock = [1000.0]
        reg = PendingRegistry(ttl_seconds=0, clock=lambda: clock[0])
        reg.register(request_id="rid-1", kind="clarify")
        clock[0] = 999999
        entry = reg.lookup("rid-1")
        assert entry.state == "pending"


class TestPendingRegistryHelpers:
    def test_is_pending(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        assert reg.is_pending("rid-1") is True
        reg.mark_resolved("rid-1", choice="yes")
        assert reg.is_pending("rid-1") is False

    def test_is_known(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        assert reg.is_known("rid-1") is True
        reg.mark_resolved("rid-1", choice="yes")
        assert reg.is_known("rid-1") is True
        assert reg.is_known("nope") is False

    def test_resolved_choice(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        assert reg.resolved_choice("rid-1") is _MISSING
        reg.mark_resolved("rid-1", choice="yes")
        assert reg.resolved_choice("rid-1") == "yes"

    def test_clear_single(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        reg.register(request_id="rid-2", kind="sudo")
        reg.clear("rid-1")
        assert reg.lookup("rid-1") is None
        assert reg.lookup("rid-2") is not None

    def test_clear_all(self):
        reg = PendingRegistry()
        reg.register(request_id="rid-1", kind="clarify")
        reg.register(request_id="rid-2", kind="sudo")
        reg.clear()
        assert reg.lookup("rid-1") is None
        assert reg.lookup("rid-2") is None


class TestPendingRegistryEvents:
    def test_requested_event_on_register(self):
        events = []
        reg = PendingRegistry(publish_event=lambda et, e: events.append((et, e)))
        reg.register(request_id="rid-1", kind="clarify")
        assert len(events) == 1
        assert events[0][0] == "interaction.requested"
        assert events[0][1].request_id == "rid-1"

    def test_resolved_event_on_mark_resolved(self):
        events = []
        reg = PendingRegistry(publish_event=lambda et, e: events.append((et, e)))
        reg.register(request_id="rid-1", kind="clarify")
        reg.mark_resolved("rid-1", choice="yes")
        assert events[-1][0] == "interaction.resolved"
        assert events[-1][1].choice == "yes"

    def test_expired_event_on_lazy_expiry(self):
        events = []
        clock = [1000.0]
        reg = PendingRegistry(
            ttl_seconds=10, clock=lambda: clock[0],
            publish_event=lambda et, e: events.append((et, e)),
        )
        reg.register(request_id="rid-1", kind="clarify")
        clock[0] = 1020
        reg.lookup("rid-1")  # triggers expiry
        assert events[-1][0] == "interaction.expired"

    def test_publish_failure_raises_and_does_not_register(self):
        def bad_publish(et, e):
            raise RuntimeError("boom")

        reg = PendingRegistry(publish_event=bad_publish)
        with pytest.raises(RuntimeError, match="boom"):
            reg.register(request_id="rid-1", kind="clarify")
        assert reg.lookup("rid-1") is None

    def test_resolve_publish_failure_raises_and_keeps_pending(self):
        events = []

        def publish(et, e):
            if et == "interaction.resolved":
                raise RuntimeError("boom")
            events.append((et, e.request_id, e.state))

        reg = PendingRegistry(publish_event=publish)
        reg.register(request_id="rid-1", kind="clarify")
        with pytest.raises(RuntimeError, match="boom"):
            reg.mark_resolved("rid-1", choice="yes")
        entry = reg.lookup("rid-1")
        assert entry is not None
        assert entry.state == "pending"

    def test_expire_publish_failure_raises_and_keeps_pending(self):
        def publish(et, e):
            if et == "interaction.expired":
                raise RuntimeError("boom")

        clock = [1000.0]
        reg = PendingRegistry(ttl_seconds=10, clock=lambda: clock[0], publish_event=publish)
        reg.register(request_id="rid-1", kind="clarify")
        clock[0] = 1020
        with pytest.raises(RuntimeError, match="boom"):
            reg.lookup("rid-1")
        clock[0] = 1000
        entry = reg.lookup("rid-1")
        assert entry is not None
        assert entry.state == "pending"


# =========================================================================
# respond three-state contract (in-process @method handlers)
# =========================================================================

def _get_method(name: str):
    """Fetch a registered @method handler from server._methods."""
    from tui_gateway import server

    return server._methods[name]


def _register_inprocess_pending(request_id: str, sid: str = "test-sid") -> threading.Event:
    """Simulate ``_block`` registering a pending request in ``server._pending``
    AND the PendingRegistry (so TTL/expiry is tracked from creation time)."""
    from tui_gateway import server

    ev = threading.Event()
    with server._prompt_lock:
        server._pending[request_id] = (sid, ev)
    # Register in the PendingRegistry so the state machine tracks TTL from
    # creation. This mirrors what the real _block path does via the bridge.
    reg = getattr(server, "_interactive_registry", None)
    if reg is not None:
        reg.register(request_id=request_id, kind="sudo", conversation_id=sid)
    return ev


class TestRespondThreeStateContract:
    """The core I8 contract: pending → resolved, already_resolved, unknown."""

    def test_respond_pending_returns_resolved(self):
        """respond to a pending request → {status:"resolved", resolved:1}."""
        ev = _register_inprocess_pending("rid-1")
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "rid-1", "password": "hunter2"})
        assert result["result"]["status"] == "resolved"
        assert result["result"]["resolved"] == 1
        assert ev.is_set()

    def test_respond_already_resolved_returns_4409(self):
        """respond to an already-resolved request → 4409 already_resolved."""
        _register_inprocess_pending("rid-1")
        handler = _get_method("sudo.respond")
        # First respond succeeds
        handler(1, {"request_id": "rid-1", "password": "pw1"})
        # The _block finally block would pop _pending, but the registry
        # remembers the resolved state. Simulate the pop:
        from tui_gateway import server
        with server._prompt_lock:
            server._pending.pop("rid-1", None)
        # Second respond → 4409
        result = handler(2, {"request_id": "rid-1", "password": "pw2"})
        assert result["error"]["code"] == 4409
        assert "already_resolved" in result["error"]["message"]

    def test_respond_already_resolved_includes_previous_choice(self):
        """4409 response includes resolved_choice when available."""
        _register_inprocess_pending("rid-1")
        handler = _get_method("secret.respond")
        handler(1, {"request_id": "rid-1", "value": "secret-value"})
        from tui_gateway import server
        with server._prompt_lock:
            server._pending.pop("rid-1", None)
        result = handler(2, {"request_id": "rid-1", "value": "other"})
        assert result["error"]["code"] == 4409
        assert result["error"]["data"]["resolved_choice"] == "secret-value"

    def test_respond_unknown_returns_4404(self):
        """respond to a never-seen request_id → 4404 unknown_request."""
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "never-seen", "password": "pw"})
        assert result["error"]["code"] == 4404
        assert result["error"]["message"] == "unknown_request"

    def test_respond_does_not_return_legacy_ok_status(self):
        """The old {status:"ok"} success path is gone."""
        _register_inprocess_pending("rid-1")
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "rid-1", "password": "pw"})
        assert result["result"]["status"] != "ok"

    def test_respond_does_not_return_resolved_zero(self):
        """The false-success {resolved:0} path is gone — miss returns 4404."""
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "nope", "password": "pw"})
        assert "result" not in result or result.get("result", {}).get("resolved") != 0
        assert result["error"]["code"] == 4404


class TestRespondExpiredState:
    """Lazy TTL expiry: a pending request past TTL → expired → 4404."""

    def test_respond_expired_returns_4404(self):
        from tui_gateway import server

        # Create a registry with a controllable clock
        clock = [1000.0]
        reg = PendingRegistry(ttl_seconds=10, clock=lambda: clock[0])
        server._interactive_registry = reg

        _register_inprocess_pending("rid-1")
        # Advance past TTL
        clock[0] = 1020
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "rid-1", "password": "pw"})
        assert result["error"]["code"] == 4404
        assert result["error"]["message"] == "unknown_request"

    def test_respond_within_ttl_succeeds(self):
        from tui_gateway import server

        clock = [1000.0]
        reg = PendingRegistry(ttl_seconds=100, clock=lambda: clock[0])
        server._interactive_registry = reg

        ev = _register_inprocess_pending("rid-1")
        clock[0] = 1050  # within TTL
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "rid-1", "password": "pw"})
        assert result["result"]["status"] == "resolved"
        assert ev.is_set()


# =========================================================================
# clarify.respond — silent swallow deletion
# =========================================================================

class TestClarifyRespondNoSilentSwallow:
    """PR-5: _respond_gateway_clarify never returns None (silent swallow).

    A miss now returns an explicit 4404 unknown_request instead of
    falling through to _respond (which would return {status:"ok"} for
    a miss it couldn't distinguish from a hit).
    """

    def test_clarify_respond_unknown_returns_4404_not_none(self):
        handler = _get_method("clarify.respond")
        result = handler(1, {"request_id": "unknown-clarify", "answer": "yes"})
        # Must be an error dict, not None
        assert result is not None
        assert "error" in result
        assert result["error"]["code"] == 4404

    def test_clarify_respond_success_returns_resolved(self):
        from tui_gateway import server

        server._interactive_registry = PendingRegistry()
        # Register a clarify entry in the clarify gateway
        clarify_module.register("cid-1", "session-key-1", "Pick one", ["A", "B"])
        handler = _get_method("clarify.respond")
        result = handler(1, {"request_id": "cid-1", "answer": "A"})
        assert result["result"]["status"] == "resolved"
        assert result["result"]["resolved"] == 1

    def test_clarify_respond_already_resolved_returns_4409(self):
        from tui_gateway import server

        server._interactive_registry = PendingRegistry()
        clarify_module.register("cid-1", "session-key-1", "Pick one", ["A", "B"])
        handler = _get_method("clarify.respond")
        # First respond succeeds
        handler(1, {"request_id": "cid-1", "answer": "A"})
        # Second respond → 4409 (clarify gateway already consumed the entry,
        # so resolve_gateway_clarify returns False → but the registry
        # remembers the resolved state)
        result = handler(2, {"request_id": "cid-1", "answer": "B"})
        assert result["error"]["code"] == 4409

    def test_clarify_respond_empty_request_id_returns_4404(self):
        handler = _get_method("clarify.respond")
        result = handler(1, {"request_id": "", "answer": "yes"})
        assert result["error"]["code"] == 4404


# =========================================================================
# approval.respond — request_id addressing
# =========================================================================

class TestApprovalRespondRequestId:
    """PR-5: approval.respond resolves by request_id via
    find_gateway_approval_by_request_id."""

    def _create_blocked_approval(self, session_key: str = "sess-1", request_id: str = "appr-1") -> approval_module._ApprovalEntry:
        """Create a real _ApprovalEntry in the gateway queue + request index."""
        data = {"command": "rm -rf /tmp", "description": "dangerous", "request_id": request_id}
        entry = approval_module._ApprovalEntry(data)
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, entry)
        return entry

    def test_approval_respond_by_request_id_succeeds(self):
        entry = self._create_blocked_approval(request_id="appr-1")
        handler = _get_method("approval.respond")
        result = handler(1, {"request_id": "appr-1", "choice": "once"})
        assert result["result"]["status"] == "resolved"
        assert result["result"]["resolved"] == 1
        assert entry.event.is_set()
        assert entry.result == "once"

    def test_approval_respond_relays_bounded_deny_reason(self):
        entry = self._create_blocked_approval(request_id="appr-reason")
        handler = _get_method("approval.respond")
        result = handler(1, {
            "request_id": "appr-reason",
            "choice": "deny",
            "reason": "use staging instead",
        })
        assert result["result"]["status"] == "resolved"
        assert entry.result == "deny"
        assert entry.reason == "use staging instead"

    def test_approval_respond_already_resolved_returns_4409(self):
        self._create_blocked_approval(request_id="appr-1")
        handler = _get_method("approval.respond")
        # First respond succeeds
        handler(1, {"request_id": "appr-1", "choice": "once"})
        # Second respond → 4409
        result = handler(2, {"request_id": "appr-1", "choice": "deny"})
        assert result["error"]["code"] == 4409
        assert result["error"]["data"]["resolved_choice"] == "once"

    def test_approval_respond_unknown_request_id_returns_4404(self):
        handler = _get_method("approval.respond")
        result = handler(1, {"request_id": "never-seen", "choice": "deny"})
        assert result["error"]["code"] == 4404
        assert result["error"]["message"] == "unknown_request"

    def test_approval_respond_resolves_all(self):
        self._create_blocked_approval(session_key="sess-1", request_id="appr-1")
        self._create_blocked_approval(session_key="sess-1", request_id="appr-2")
        handler = _get_method("approval.respond")
        result = handler(1, {"request_id": "appr-1", "choice": "session", "all": True})
        assert result["result"]["status"] == "resolved"
        assert result["result"]["resolved"] >= 1

    def test_approval_respond_no_request_id_falls_back_to_session(self):
        """Legacy path: no request_id → session_key resolution."""
        from tui_gateway import server
        server._sessions["sess-1"] = {"session_key": "sess-1"}
        try:
            entry = self._create_blocked_approval(session_key="sess-1", request_id="appr-1")
            handler = _get_method("approval.respond")
            # No request_id, but session_id matches the session_key
            result = handler(1, {"session_id": "sess-1", "choice": "once"})
            assert result["result"]["status"] == "resolved"
            assert entry.event.is_set()
        finally:
            server._sessions.pop("sess-1", None)

    def test_approval_respond_no_pending_returns_4404_not_resolved_zero(self):
        """PR-5: the false-success {resolved:0} path is deleted."""
        handler = _get_method("approval.respond")
        result = handler(1, {"request_id": "no-such-approval", "choice": "deny"})
        assert result["error"]["code"] == 4404
        # Must NOT be a success with resolved:0
        assert result.get("result", {}).get("resolved") != 0


# =========================================================================
# has_blocking_approval pre-check deletion
# =========================================================================

class TestHasBlockingApprovalPrecheckDeleted:
    """PR-5: the has_blocking_approval fast-path is removed.

    The old code called ``has_blocking_approval(session_key)`` and, if
    True, returned ``_ok({resolved: resolve_gateway_approval(...)})``
    BEFORE the registry could track the state. This made a second
    respond look like a fresh success (resolved:N) instead of 4409.
    """

    def test_second_respond_returns_4409_not_success(self):
        from tui_gateway import server
        server._sessions["sess-1"] = {"session_key": "sess-1"}
        try:
            data = {"command": "rm -rf /tmp", "request_id": "appr-1"}
            entry = approval_module._ApprovalEntry(data)
            with approval_module._lock:
                approval_module._index_gateway_entry_locked("sess-1", entry)
            handler = _get_method("approval.respond")
            # First respond by session_id (the old fast-path trigger)
            r1 = handler(1, {"session_id": "sess-1", "choice": "once"})
            assert r1["result"]["status"] == "resolved"
            # Second respond by the SAME session_id — old code would have
            # returned {resolved:0} (has_blocking_approval=False → fall to
            # _approval_session_key → resolve_gateway_approval returns 0).
            # PR-5 returns 4404 (nothing pending).
            r2 = handler(2, {"session_id": "sess-1", "choice": "deny"})
            assert r2["error"]["code"] == 4404
        finally:
            server._sessions.pop("sess-1", None)


# =========================================================================
# interaction.* canonical events
# =========================================================================

class TestInteractionEvents:
    """interaction.requested / resolved / expired events are published."""

    def test_resolved_event_published_on_successful_respond(self):
        from tui_gateway import server

        events = []
        reg = PendingRegistry(
            publish_event=lambda et, e: events.append((et, e.request_id, e.state)),
        )
        server._interactive_registry = reg

        _register_inprocess_pending("rid-1")
        handler = _get_method("sudo.respond")
        handler(1, {"request_id": "rid-1", "password": "pw"})
        # Should have requested (lazy register) + resolved events
        event_types = [e[0] for e in events]
        assert "interaction.requested" in event_types
        assert "interaction.resolved" in event_types

    def test_resolved_event_published_on_clarify_respond(self):
        from tui_gateway import server

        events = []
        reg = PendingRegistry(
            publish_event=lambda et, e: events.append((et, e.request_id, e.state)),
        )
        server._interactive_registry = reg

        clarify_module.register("cid-1", "session-key-1", "Pick one", ["A", "B"])
        handler = _get_method("clarify.respond")
        handler(1, {"request_id": "cid-1", "answer": "A"})
        event_types = [e[0] for e in events]
        assert "interaction.resolved" in event_types

    def test_expired_event_published_on_ttl_expiry(self):
        from tui_gateway import server

        events = []
        clock = [1000.0]
        reg = PendingRegistry(
            ttl_seconds=10, clock=lambda: clock[0],
            publish_event=lambda et, e: events.append((et, e.request_id, e.state)),
        )
        server._interactive_registry = reg

        _register_inprocess_pending("rid-1")
        clock[0] = 1020  # past TTL
        handler = _get_method("sudo.respond")
        handler(1, {"request_id": "rid-1", "password": "pw"})
        # The respond triggers a registry lookup which flips to expired
        event_types = [e[0] for e in events]
        assert "interaction.expired" in event_types


# =========================================================================
# Error code consistency
# =========================================================================

class TestErrorCodes:
    """4404/4409 are dedicated to the interactive-respond contract."""

    def test_miss_uses_4404_not_4009(self):
        """The old miss code was 4009; PR-5 uses 4404 for unknown_request."""
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "nope", "password": "pw"})
        assert result["error"]["code"] == 4404
        assert result["error"]["code"] != 4009

    def test_already_resolved_uses_4409(self):
        _register_inprocess_pending("rid-1")
        handler = _get_method("sudo.respond")
        handler(1, {"request_id": "rid-1", "password": "pw1"})
        from tui_gateway import server
        with server._prompt_lock:
            server._pending.pop("rid-1", None)
        result = handler(2, {"request_id": "rid-1", "password": "pw2"})
        assert result["error"]["code"] == 4409

    def test_success_uses_resolved_not_ok(self):
        _register_inprocess_pending("rid-1")
        handler = _get_method("sudo.respond")
        result = handler(1, {"request_id": "rid-1", "password": "pw"})
        assert result["result"]["status"] == "resolved"
        assert result["result"]["status"] != "ok"
