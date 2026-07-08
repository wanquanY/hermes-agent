"""PR-4 tests: interactive request_id full-path identity.

Covers the three modules touched by PR-4 (interactive request identity):

* ``tools/approval`` — ``mint_request_id`` / ``ensure_request_id`` /
  ``_ApprovalEntry`` auto-mint, the ``_gateway_request_index`` round-trip
  (``_index_gateway_entry_locked`` / ``_unindex_gateway_entry_locked``),
  ``find_gateway_approval_by_request_id`` (incl. None edge cases), and the
  ``resolve_gateway_approval`` request_id-addressing branch.
* ``tools/clarify_gateway`` — ``register()`` backstop-mint when handed an
  empty ``clarify_id``; ``_ClarifyEntry.signature`` now carries
  ``request_id``.
* ``tui_gateway/services/worker_publish_bridge.py`` —
  ``register_interactive_request`` idempotency / kind validation / empty
  request_id rejection, and ``_register_approval_request`` backstop mint.

Style follows ``tests/tools/test_approval.py`` (import the module by name,
clear module-level state under the module lock between tests).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

import pytest

import tools.approval as approval_module
import tools.clarify_gateway as clarify_module
from tui_gateway.run_worker import InteractiveRequestFrame
from tui_gateway.services.worker_publish_bridge import WorkerPublishBridge


# =========================================================================
# Helpers — state cleanup
# =========================================================================

def _clear_approval_globals() -> None:
    """Wipe every piece of approval module-level state PR-4 touches.

    Held under the module lock so concurrent test collection can't observe
    a half-cleared state.
    """
    with approval_module._lock:
        approval_module._gateway_queues.clear()
        approval_module._gateway_request_index.clear()
        approval_module._gateway_notify_cbs.clear()
        approval_module._pending.clear()
        approval_module._session_approved.clear()
        approval_module._session_yolo.clear()


def _clear_clarify_globals() -> None:
    """Wipe clarify_gateway module-level state."""
    with clarify_module._lock:
        clarify_module._entries.clear()
        clarify_module._session_index.clear()


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset all three modules' global state before each test."""
    _clear_approval_globals()
    _clear_clarify_globals()
    yield
    _clear_approval_globals()
    _clear_clarify_globals()


# =========================================================================
# approval: mint_request_id
# =========================================================================

class TestMintRequestId:
    def test_returns_uuid4_hex(self):
        rid = approval_module.mint_request_id()
        # uuid4().hex is 32 lowercase hex chars, no dashes.
        assert isinstance(rid, str)
        assert len(rid) == 32
        # Must round-trip through uuid.UUID.
        parsed = uuid.UUID(rid)
        assert parsed.hex == rid
        # uuid4 variant bit set (top two bits of byte 8 are 10).
        assert parsed.variant == uuid.RFC_4122

    def test_unique_across_calls(self):
        ids = {approval_module.mint_request_id() for _ in range(100)}
        assert len(ids) == 100

    def test_not_empty(self):
        assert approval_module.mint_request_id() != ""


# =========================================================================
# approval: ensure_request_id
# =========================================================================

class TestEnsureRequestId:
    def test_mints_when_absent_and_mutates_dict(self):
        data = {"command": "rm -rf /tmp"}
        rid = approval_module.ensure_request_id(data)
        assert isinstance(rid, str) and rid
        # Mutation: the id is stamped back into the dict.
        assert data["request_id"] == rid

    def test_preserves_existing_id(self):
        existing = "abc123"
        data = {"command": "rm -rf /tmp", "request_id": existing}
        rid = approval_module.ensure_request_id(data)
        assert rid == existing
        assert data["request_id"] == existing

    def test_preserves_existing_id_with_whitespace(self):
        data = {"request_id": "  keepme  "}
        rid = approval_module.ensure_request_id(data)
        # .strip() is applied when reading, but the stored value is unchanged
        # when a non-empty id was already present (mint only fires on falsy).
        assert rid == "keepme"

    def test_mints_when_empty_string(self):
        data = {"request_id": ""}
        rid = approval_module.ensure_request_id(data)
        assert rid
        assert data["request_id"] == rid

    def test_mints_when_whitespace_only(self):
        data = {"request_id": "   "}
        rid = approval_module.ensure_request_id(data)
        assert rid and rid != "   "
        assert data["request_id"] == rid

    def test_mints_when_none_value(self):
        data = {"request_id": None}
        rid = approval_module.ensure_request_id(data)
        assert rid
        assert data["request_id"] == rid

    def test_non_dict_returns_empty(self):
        assert approval_module.ensure_request_id(None) == ""  # type: ignore[arg-type]
        assert approval_module.ensure_request_id("not-a-dict") == ""  # type: ignore[arg-type]
        assert approval_module.ensure_request_id(42) == ""  # type: ignore[arg-type]
        assert approval_module.ensure_request_id(["list"]) == ""  # type: ignore[arg-type]

    def test_idempotent_on_second_call(self):
        data = {"command": "x"}
        rid1 = approval_module.ensure_request_id(data)
        rid2 = approval_module.ensure_request_id(data)
        assert rid1 == rid2
        assert data["request_id"] == rid1


# =========================================================================
# approval: _ApprovalEntry auto-mint
# =========================================================================

class TestApprovalEntryMint:
    def test_init_mints_request_id_into_data(self):
        data = {"command": "rm -rf /tmp"}
        entry = approval_module._ApprovalEntry(data)
        assert entry.request_id
        assert data["request_id"] == entry.request_id

    def test_init_preserves_existing_request_id(self):
        data = {"command": "rm", "request_id": "pre-existing"}
        entry = approval_module._ApprovalEntry(data)
        assert entry.request_id == "pre-existing"
        assert data["request_id"] == "pre-existing"

    def test_two_entries_get_distinct_ids(self):
        e1 = approval_module._ApprovalEntry({"command": "a"})
        e2 = approval_module._ApprovalEntry({"command": "b"})
        assert e1.request_id != e2.request_id

    def test_event_initially_unset(self):
        entry = approval_module._ApprovalEntry({"command": "x"})
        assert entry.event.is_set() is False

    def test_result_initially_none(self):
        entry = approval_module._ApprovalEntry({"command": "x"})
        assert entry.result is None


# =========================================================================
# approval: index / unindex round-trip (caller holds lock)
# =========================================================================

class TestIndexUnindexRoundTrip:
    def test_index_populates_queue_and_request_index(self):
        session_key = "sess-index-1"
        entry = approval_module._ApprovalEntry({"command": "rm"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, entry)
        assert approval_module._gateway_queues[session_key] == [entry]
        assert approval_module._gateway_request_index[entry.request_id] == (
            session_key,
            entry,
        )

    def test_unindex_removes_request_index(self):
        session_key = "sess-index-2"
        entry = approval_module._ApprovalEntry({"command": "rm"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, entry)
            approval_module._unindex_gateway_entry_locked(entry)
        assert entry.request_id not in approval_module._gateway_request_index

    def test_unindex_does_not_remove_other_entry_with_same_id(self):
        # Two entries cannot share a request_id in practice (ids are uuid4),
        # but _unindex checks identity (indexed[1] is entry) before popping.
        session_key = "sess-index-3"
        e1 = approval_module._ApprovalEntry({"command": "a"})
        e2 = approval_module._ApprovalEntry({"command": "b"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, e1)
            approval_module._index_gateway_entry_locked(session_key, e2)
            # Overwrite the index so e1's id points at e2 (simulate stale
            # entry still in queue but index now pointing elsewhere).
            approval_module._gateway_request_index[e1.request_id] = (session_key, e2)
            approval_module._unindex_gateway_entry_locked(e1)
        # e1's id still indexed (points at e2) — unindex must NOT pop it.
        assert approval_module._gateway_request_index[e1.request_id] == (session_key, e2)

    def test_unindex_on_never_indexed_entry_is_noop(self):
        entry = approval_module._ApprovalEntry({"command": "x"})
        with approval_module._lock:
            approval_module._unindex_gateway_entry_locked(entry)
        assert entry.request_id not in approval_module._gateway_request_index

    def test_index_multiple_sessions(self):
        e1 = approval_module._ApprovalEntry({"command": "a"})
        e2 = approval_module._ApprovalEntry({"command": "b"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked("s1", e1)
            approval_module._index_gateway_entry_locked("s2", e2)
        assert approval_module._gateway_request_index[e1.request_id][0] == "s1"
        assert approval_module._gateway_request_index[e2.request_id][0] == "s2"


# =========================================================================
# approval: find_gateway_approval_by_request_id
# =========================================================================

class TestFindGatewayApprovalByRequestId:
    def test_finds_indexed_entry(self):
        session_key = "sess-find-1"
        data = {"command": "rm -rf /tmp", "description": "danger"}
        entry = approval_module._ApprovalEntry(data)
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, entry)

        result = approval_module.find_gateway_approval_by_request_id(entry.request_id)
        assert result is not None
        assert result["session_key"] == session_key
        # data is a *copy* (dict(...)) — mutating it must not touch the entry.
        assert result["data"]["command"] == "rm -rf /tmp"
        assert result["data"]["request_id"] == entry.request_id

    def test_returns_copy_not_reference(self):
        entry = approval_module._ApprovalEntry({"command": "rm"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked("sess", entry)
        result = approval_module.find_gateway_approval_by_request_id(entry.request_id)
        assert result is not None
        result["data"]["command"] = "MUTATED"
        # Original entry.data untouched.
        assert entry.data["command"] == "rm"

    def test_returns_none_for_unknown_id(self):
        assert approval_module.find_gateway_approval_by_request_id("no-such-id") is None

    def test_returns_none_for_empty_string(self):
        assert approval_module.find_gateway_approval_by_request_id("") is None

    def test_returns_none_for_none(self):
        assert approval_module.find_gateway_approval_by_request_id(None) is None  # type: ignore[arg-type]

    def test_returns_none_for_whitespace(self):
        assert approval_module.find_gateway_approval_by_request_id("   ") is None

    def test_returns_none_after_unindex(self):
        entry = approval_module._ApprovalEntry({"command": "rm"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked("sess", entry)
            approval_module._unindex_gateway_entry_locked(entry)
        assert approval_module.find_gateway_approval_by_request_id(entry.request_id) is None


# =========================================================================
# approval: resolve_gateway_approval — request_id addressing branch
# =========================================================================

class TestResolveGatewayApprovalRequestIdAddressing:
    def test_resolve_by_request_id_resolves_exact_entry(self):
        session_key = "sess-resolve-rid-1"
        entry = approval_module._ApprovalEntry({"command": "rm"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, entry)

        count = approval_module.resolve_gateway_approval(entry.request_id, "once")
        assert count == 1
        assert entry.result == "once"
        assert entry.event.is_set()

    def test_resolve_by_request_id_removes_from_owner_queue(self):
        session_key = "sess-resolve-rid-2"
        entry = approval_module._ApprovalEntry({"command": "rm"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, entry)

        approval_module.resolve_gateway_approval(entry.request_id, "deny")
        # Queue for the *owner* session is now empty / gone.
        assert approval_module._gateway_queues.get(session_key) in (None, [])
        # Index dropped.
        assert entry.request_id not in approval_module._gateway_request_index

    def test_resolve_by_request_id_returns_zero_for_unknown(self):
        assert approval_module.resolve_gateway_approval("unknown-rid", "once") == 0

    def test_resolve_by_request_id_only_resolves_target_not_others(self):
        """When a session has two pending entries, resolving by one entry's
        request_id must leave the other pending."""
        session_key = "sess-resolve-rid-3"
        e1 = approval_module._ApprovalEntry({"command": "a"})
        e2 = approval_module._ApprovalEntry({"command": "b"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, e1)
            approval_module._index_gateway_entry_locked(session_key, e2)

        count = approval_module.resolve_gateway_approval(e1.request_id, "once")
        assert count == 1
        assert e1.event.is_set()
        assert e1.result == "once"
        # e2 still pending.
        assert not e2.event.is_set()
        assert e2.result is None
        assert approval_module._gateway_queues.get(session_key) == [e2]
        assert e2.request_id in approval_module._gateway_request_index

    def test_session_key_fifo_takes_precedence_over_request_id(self):
        """When the session_key argument matches a real session queue, the
        FIFO branch is used (request_id addressing is the fallback)."""
        session_key = "sess-fifo-precedence"
        e1 = approval_module._ApprovalEntry({"command": "a"})
        e2 = approval_module._ApprovalEntry({"command": "b"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, e1)
            approval_module._index_gateway_entry_locked(session_key, e2)

        # Resolve by session_key (FIFO) — resolves oldest = e1.
        count = approval_module.resolve_gateway_approval(session_key, "once")
        assert count == 1
        assert e1.event.is_set()
        assert not e2.event.is_set()

    def test_resolve_all_by_session_key(self):
        session_key = "sess-resolve-all"
        e1 = approval_module._ApprovalEntry({"command": "a"})
        e2 = approval_module._ApprovalEntry({"command": "b"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(session_key, e1)
            approval_module._index_gateway_entry_locked(session_key, e2)

        count = approval_module.resolve_gateway_approval(
            session_key, "session", resolve_all=True
        )
        assert count == 2
        assert e1.event.is_set() and e2.event.is_set()
        assert e1.result == "session" and e2.result == "session"
        assert session_key not in approval_module._gateway_queues

    def test_resolve_by_request_id_does_not_touch_other_session(self):
        s1 = "sess-isolation-1"
        s2 = "sess-isolation-2"
        e1 = approval_module._ApprovalEntry({"command": "a"})
        e2 = approval_module._ApprovalEntry({"command": "b"})
        with approval_module._lock:
            approval_module._index_gateway_entry_locked(s1, e1)
            approval_module._index_gateway_entry_locked(s2, e2)

        approval_module.resolve_gateway_approval(e1.request_id, "once")
        assert e1.event.is_set()
        assert not e2.event.is_set()
        # s2's queue + index intact.
        assert approval_module._gateway_queues.get(s2) == [e2]
        assert e2.request_id in approval_module._gateway_request_index


# =========================================================================
# clarify_gateway: register() backstop-mint + signature
# =========================================================================

class TestClarifyRegisterBackstop:
    def test_empty_clarify_id_is_backstop_minted(self):
        entry = clarify_module.register("", "sess-clarify-1", "which?", ["a", "b"])
        assert entry.clarify_id
        # Minted id is a uuid4 hex (32 chars).
        assert len(entry.clarify_id) == 32
        assert entry.clarify_id in clarify_module._entries

    def test_none_clarify_id_is_backstop_minted(self):
        entry = clarify_module.register(None, "sess-clarify-2", "q?", None)  # type: ignore[arg-type]
        assert entry.clarify_id
        assert len(entry.clarify_id) == 32

    def test_whitespace_clarify_id_is_backstop_minted(self):
        entry = clarify_module.register("   ", "sess-clarify-3", "q?", None)
        assert entry.clarify_id
        assert len(entry.clarify_id) == 32
        assert entry.clarify_id != "   "

    def test_explicit_clarify_id_is_preserved(self):
        explicit = "user-supplied-id-123"
        entry = clarify_module.register(explicit, "sess-clarify-4", "q?", ["x"])
        assert entry.clarify_id == explicit

    def test_backstop_mint_ids_are_unique(self):
        ids = {
            clarify_module.register("", f"sess-{i}", "q?", None).clarify_id
            for i in range(20)
        }
        assert len(ids) == 20

    def test_register_stores_entry_and_session_index(self):
        entry = clarify_module.register("", "sess-clarify-idx", "q?", ["a"])
        assert clarify_module._entries[entry.clarify_id] is entry
        assert entry.clarify_id in clarify_module._session_index["sess-clarify-idx"]


class TestClarifySignatureRequestId:
    def test_signature_includes_request_id(self):
        entry = clarify_module.register("cid-1", "sess-sig-1", "pick one", ["a", "b"])
        sig = entry.signature()
        assert "request_id" in sig
        assert sig["request_id"] == entry.clarify_id
        assert sig["request_id"] == sig["clarify_id"]

    def test_signature_request_id_equals_clarify_id_for_backstop_mint(self):
        entry = clarify_module.register("", "sess-sig-2", "q?", None)
        sig = entry.signature()
        assert sig["request_id"] == entry.clarify_id
        assert len(sig["request_id"]) == 32

    def test_signature_preserves_other_fields(self):
        entry = clarify_module.register("cid-2", "sess-sig-3", "the question", ["a", "b"])
        sig = entry.signature()
        assert sig["clarify_id"] == "cid-2"
        assert sig["session_key"] == "sess-sig-3"
        assert sig["question"] == "the question"
        assert sig["choices"] == ["a", "b"]

    def test_signature_choices_none_when_open_ended(self):
        entry = clarify_module.register("cid-3", "sess-sig-4", "q?", None)
        sig = entry.signature()
        assert sig["choices"] is None
        assert sig["request_id"] == "cid-3"


# =========================================================================
# worker_publish_bridge: register_interactive_request
# =========================================================================

def _make_bridge(conversation_session_id: str = "stored-1") -> tuple[WorkerPublishBridge, list]:
    """Build a WorkerPublishBridge with a recording emit_threadsafe.

    Returns (bridge, emitted_frames) where ``emitted_frames`` collects every
    frame handed to emit_threadsafe. We bypass the real asyncio loop so no
    coroutine is actually scheduled.
    """
    loop = SimpleNamespace(is_closed=lambda: False)
    bridge = WorkerPublishBridge(emit=_noop_emit, loop=loop)  # type: ignore[arg-type]
    bridge._conversation_session_id = conversation_session_id
    emitted: list = []

    def _record(frame):
        emitted.append(frame)

    bridge.emit_threadsafe = _record  # type: ignore[method-assign]
    return bridge, emitted


async def _noop_emit(frame):
    pass


class TestRegisterInteractiveRequest:
    def test_emits_frame_and_returns_true(self):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request(
            "req-1", "approval", {"command": "rm"}, conversation_session_id="sess-1"
        )
        assert ok is True
        assert len(emitted) == 1
        frame = emitted[0]
        assert isinstance(frame, InteractiveRequestFrame)
        assert frame.kind == "approval"
        assert frame.request_id == "req-1"
        assert frame.payload == {"command": "rm"}
        assert frame.conversation_session_id == "sess-1"

    def test_uses_bridge_conversation_session_id_when_arg_empty(self):
        bridge, emitted = _make_bridge(conversation_session_id="bridge-default")
        bridge.register_interactive_request("req-2", "clarify")
        assert emitted[0].conversation_session_id == "bridge-default"

    def test_payload_defaults_to_empty_dict(self):
        bridge, emitted = _make_bridge()
        bridge.register_interactive_request("req-3", "secret")
        assert emitted[0].payload == {}

    def test_payload_copied_not_referenced(self):
        bridge, emitted = _make_bridge()
        payload = {"k": "v"}
        bridge.register_interactive_request("req-4", "approval", payload)
        # Mutate original; emitted frame's payload must be unaffected.
        payload["k"] = "mutated"
        assert emitted[0].payload == {"k": "v"}

    def test_idempotent_same_request_id(self):
        """Calling twice with the same request_id emits exactly one frame."""
        bridge, emitted = _make_bridge()
        ok1 = bridge.register_interactive_request("req-dup", "approval", {"a": 1})
        ok2 = bridge.register_interactive_request("req-dup", "approval", {"a": 2})
        assert ok1 is True
        assert ok2 is True
        assert len(emitted) == 1
        # First payload wins (second is suppressed by the dedup set).
        assert emitted[0].payload == {"a": 1}

    def test_idempotent_across_kinds(self):
        """Dedup is by request_id alone, even if kind differs — second call
        is suppressed (already emitted)."""
        bridge, emitted = _make_bridge()
        bridge.register_interactive_request("shared-rid", "approval")
        bridge.register_interactive_request("shared-rid", "clarify")
        assert len(emitted) == 1
        assert emitted[0].kind == "approval"

    def test_different_request_ids_each_emit(self):
        bridge, emitted = _make_bridge()
        bridge.register_interactive_request("rid-a", "approval")
        bridge.register_interactive_request("rid-b", "clarify")
        assert len(emitted) == 2
        assert {f.request_id for f in emitted} == {"rid-a", "rid-b"}

    def test_rejects_unknown_kind(self):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request("req-x", "unknown_kind", {"a": 1})
        assert ok is False
        assert emitted == []

    @pytest.mark.parametrize("bad_kind", ["", "   ", "APPROVAL", "Approval", "file", "tool"])
    def test_rejects_various_invalid_kinds(self, bad_kind):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request("req-k", bad_kind)
        assert ok is False
        assert emitted == []

    @pytest.mark.parametrize("valid_kind", ["approval", "clarify", "secret", "sudo"])
    def test_accepts_all_four_valid_kinds(self, valid_kind):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request(
            f"req-{valid_kind}", valid_kind
        )
        assert ok is True
        assert emitted[0].kind == valid_kind

    def test_rejects_empty_request_id(self):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request("", "approval", {"a": 1})
        assert ok is False
        assert emitted == []

    def test_rejects_whitespace_request_id(self):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request("   ", "approval")
        assert ok is False
        assert emitted == []

    def test_rejects_none_request_id(self):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request(None, "approval")  # type: ignore[arg-type]
        assert ok is False
        assert emitted == []

    def test_strips_whitespace_from_request_id(self):
        bridge, emitted = _make_bridge()
        ok = bridge.register_interactive_request("  req-trim  ", "approval")
        assert ok is True
        assert emitted[0].request_id == "req-trim"


# =========================================================================
# worker_publish_bridge: _register_approval_request backstop
# =========================================================================

class TestRegisterApprovalRequestBackstop:
    def test_backstop_mints_when_dict_has_no_request_id(self):
        bridge, emitted = _make_bridge()
        data = {"command": "rm -rf /tmp"}
        bridge._register_approval_request("sess-approval-1", data)
        assert "request_id" in data
        assert data["request_id"]
        assert len(emitted) == 1
        assert emitted[0].kind == "approval"
        assert emitted[0].request_id == data["request_id"]

    def test_uses_existing_request_id_when_present(self):
        bridge, emitted = _make_bridge()
        data = {"command": "rm", "request_id": "pre-minted"}
        bridge._register_approval_request("sess-approval-2", data)
        assert data["request_id"] == "pre-minted"
        assert emitted[0].request_id == "pre-minted"

    def test_payload_includes_session_key(self):
        bridge, emitted = _make_bridge()
        data = {"command": "rm"}
        bridge._register_approval_request("sess-approval-3", data)
        assert emitted[0].payload["session_key"] == "sess-approval-3"

    def test_payload_session_key_setdefault_does_not_overwrite(self):
        bridge, emitted = _make_bridge()
        data = {"command": "rm", "session_key": "already-here"}
        bridge._register_approval_request("sess-approval-4", data)
        assert emitted[0].payload["session_key"] == "already-here"

    def test_non_dict_payload_does_not_emit(self):
        bridge, emitted = _make_bridge()
        # Non-dict approval_data → rid stays empty, warn logged, no emit.
        bridge._register_approval_request("sess-approval-5", "not-a-dict")  # type: ignore[arg-type]
        assert emitted == []

    def test_conversation_session_id_falls_back_to_session_key(self):
        bridge, emitted = _make_bridge(conversation_session_id="")
        data = {"command": "rm"}
        bridge._register_approval_request("sess-fallback", data)
        assert emitted[0].conversation_session_id == "sess-fallback"

    def test_conversation_session_id_prefers_bridge_value(self):
        bridge, emitted = _make_bridge(conversation_session_id="bridge-sess")
        data = {"command": "rm"}
        bridge._register_approval_request("other-sess", data)
        assert emitted[0].conversation_session_id == "bridge-sess"
