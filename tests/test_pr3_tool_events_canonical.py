"""PR-3 §4.3 — canonical tool-event delivery tests.

The plan requires that snapshot/pagination responses serve tool data directly
in canonical event form (``tool.start`` / ``tool.complete`` carrying the *real*
``run_events.seq`` pair), so FE no longer reverse-derives events from the
``tool_events`` row model.  The ``tool_events`` table itself is untouched (it
remains a read model / backfill source).

These tests construct a real ``SessionDB``, append ``tool.start`` +
``tool.complete`` run_events via ``append_run_event`` (which auto-assigns
authoritative ``seq`` values), and assert that
``list_tool_events_as_canonical`` returns events whose ``seq`` is the real
``run_events.seq`` — **not** the ``tool_events.seq_start`` projection value.

They also exercise the ``session.messages`` JSON-RPC handler to verify the
canonical path is wired through the gateway without row-model fallback.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

from hermes_state import SessionDB
from hermes_agent.read_models.tool_events import list_tool_events_as_canonical
from tui_gateway import server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_event(
    event_type: str,
    *,
    seq: int,
    tool_id: str = "tool-1",
    name: str = "terminal",
    run_id: str = "run-1",
    turn_id: str = "turn-1",
    stored_session_id: str = "session-1",
    payload: dict | None = None,
) -> dict:
    """Build a minimal tool.* run_event frame suitable for ``append_run_event``."""
    body: dict[str, Any] = {
        "type": event_type,
        "session_id": f"runtime-{run_id}",
        "stored_session_id": stored_session_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "participant_id": "agent:default",
        "seq": seq,
        "timestamp": 1000.0 + seq,
        "payload": {
            "name": name,
        },
    }
    if tool_id:
        body["payload"]["tool_id"] = tool_id
    body["payload"].update(payload or {})
    return body


def _install_db(monkeypatch: Any, tmp_path: Path) -> SessionDB:
    """Build a real SessionDB and route ``_get_db`` in session_history to it."""
    session_history = importlib.import_module("tui_gateway.methods.session_history")
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(session_history, "_get_db", lambda: db)
    return db


def _call_messages(rid: str, params: dict[str, Any]) -> dict[str, Any]:
    response = server._methods["session.messages"](rid, params)
    assert "error" not in response, response
    return response["result"]


# ---------------------------------------------------------------------------
# list_tool_events_as_canonical — unit tests on the DB connection
# ---------------------------------------------------------------------------

def test_canonical_returns_tool_start_and_complete_with_real_run_events_seq(tmp_path):
    """Canonical events carry the real ``run_events.seq``, not ``seq_start``."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event(
                "tool.start",
                seq=1,
                payload={"arguments": {"command": "pwd"}},
            ),
        )
        db.append_run_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=2,
                payload={
                    "result": {"exit_code": 0},
                    "result_text": "/tmp/project",
                    "summary": "pwd completed",
                    "duration_s": 1.25,
                },
            ),
        )

        events = list_tool_events_as_canonical(db._conn, "session-1")  # noqa: SLF001
    finally:
        db.close()

    assert len(events) == 2
    assert events[0]["type"] == "tool.start"
    assert events[1]["type"] == "tool.complete"

    # The canonical seq must be the run_events.seq (1 and 2), not the
    # tool_events.seq_start projection (which collapses both frames into a
    # single row with seq_start=1).
    assert events[0]["seq"] == 1
    assert events[1]["seq"] == 2

    # run_id / turn_id propagate from the run_events row.
    assert events[0]["run_id"] == "run-1"
    assert events[0]["turn_id"] == "turn-1"
    assert events[1]["run_id"] == "run-1"
    assert events[1]["turn_id"] == "turn-1"


def test_canonical_seq_differs_from_tool_events_seq_start_when_multiple_frames_collide(tmp_path):
    """Triage S5 "dual seq identity": tool_events collapses a start+complete
    pair into one row (seq_start=earliest), but run_events keeps two distinct
    seqs.  The canonical reader must surface both real seqs."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "dovie")
        # Same tool_id → tool_events projects a single row with
        # seq_start=1, seq_last=3 (start=1, progress=2, complete=3).
        db.append_run_event(
            "session-1",
            _tool_event("tool.start", seq=1, payload={"arguments": {"command": "ls"}}),
        )
        db.append_run_event(
            "session-1",
            _tool_event("tool.progress", seq=2, payload={"preview": "listing"}),
        )
        db.append_run_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=3,
                payload={"result_text": "file1\nfile2"},
            ),
        )

        # The projection collapses to one row.
        tool_rows = db.list_tool_events("session-1")
        assert len(tool_rows) == 1
        assert tool_rows[0]["seq_start"] == 1
        assert tool_rows[0]["seq_last"] == 3

        # The canonical reader returns three distinct events with real seqs.
        canonical = list_tool_events_as_canonical(db._conn, "session-1")  # noqa: SLF001
    finally:
        db.close()

    assert [e["type"] for e in canonical] == [
        "tool.start",
        "tool.progress",
        "tool.complete",
    ]
    assert [e["seq"] for e in canonical] == [1, 2, 3]
    # All share the same run_id.
    assert all(e["run_id"] == "run-1" for e in canonical)


def test_canonical_after_seq_cursor_filters_exclusively(tmp_path):
    """``after_seq`` is an exclusive forward cursor (seq > after_seq)."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "dovie")
        for seq in range(1, 5):
            db.append_run_event(
                "session-1",
                _tool_event(
                    "tool.start",
                    seq=seq,
                    tool_id=f"tool-{seq}",
                    run_id=f"run-{seq}",
                ),
            )

        events = list_tool_events_as_canonical(  # noqa: SLF001
            db._conn, "session-1", after_seq=2
        )
    finally:
        db.close()

    assert [e["seq"] for e in events] == [3, 4]
    assert all(e["seq"] > 2 for e in events)


def test_canonical_returns_empty_for_unknown_session(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "dovie")
        events = list_tool_events_as_canonical(db._conn, "no-such-session")  # noqa: SLF001
    finally:
        db.close()
    assert events == []


def test_canonical_returns_empty_for_empty_session_id(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        events = list_tool_events_as_canonical(db._conn, "")  # noqa: SLF001
    finally:
        db.close()
    assert events == []


def test_canonical_payload_preserved_from_run_events(tmp_path):
    """The payload dict (arguments/result/summary) flows through unchanged."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event(
                "tool.start",
                seq=1,
                payload={"arguments": {"command": "echo hi"}},
            ),
        )
        db.append_run_event(
            "session-1",
            _tool_event(
                "tool.complete",
                seq=2,
                payload={
                    "result": {"exit_code": 0},
                    "result_text": "hi",
                    "summary": "echo done",
                },
            ),
        )
        events = list_tool_events_as_canonical(db._conn, "session-1")  # noqa: SLF001
    finally:
        db.close()

    assert events[0]["payload"]["arguments"] == {"command": "echo hi"}
    assert events[1]["payload"]["result"] == {"exit_code": 0}
    assert events[1]["payload"]["result_text"] == "hi"
    assert events[1]["payload"]["summary"] == "echo done"


def test_canonical_event_shape_matches_list_run_events(tmp_path):
    """Canonical tool events must have the same dict shape as list_run_events
    output — same keys, same seq source — so FE can treat them uniformly."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event("tool.start", seq=1, run_id="run-A", turn_id="turn-A"),
        )
        db.append_run_event(
            "session-1",
            _tool_event("tool.complete", seq=2, run_id="run-A", turn_id="turn-A"),
        )

        run_events = db.list_run_events("session-1")
        tool_events = db.list_tool_events("session-1")  # legacy row model
        canonical = list_tool_events_as_canonical(db._conn, "session-1")  # noqa: SLF001
    finally:
        db.close()

    # list_run_events returns ALL run_events (here: 2 tool events).
    assert len(run_events) == 2
    # canonical filters to tool events only — same 2.
    assert len(canonical) == 2

    # The canonical events must match list_run_events on the identity-bearing
    # fields (type, seq, run_id, turn_id).
    for cano, revt in zip(canonical, run_events):
        assert cano["type"] == revt["type"]
        assert cano["seq"] == revt["seq"]
        assert cano["run_id"] == revt["run_id"]
        assert cano["turn_id"] == revt["turn_id"]

    # The legacy row model collapses both into one row whose seq_start=1;
    # the canonical seqs (1, 2) are NOT equal to seq_start for the complete
    # event — this is the dual-seq identity that PR-3 fixes.
    assert len(tool_events) == 1
    assert tool_events[0]["seq_start"] == 1
    assert canonical[1]["seq"] == 2  # real run_events seq, != seq_start


def test_canonical_multiple_runs_distinct_run_ids(tmp_path):
    """Events from different runs keep their distinct run_id."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event("tool.start", seq=1, tool_id="t1", run_id="run-A"),
        )
        db.append_run_event(
            "session-1",
            _tool_event("tool.complete", seq=2, tool_id="t1", run_id="run-A"),
        )
        db.append_run_event(
            "session-1",
            _tool_event("tool.start", seq=3, tool_id="t2", run_id="run-B"),
        )
        db.append_run_event(
            "session-1",
            _tool_event("tool.complete", seq=4, tool_id="t2", run_id="run-B"),
        )
        events = list_tool_events_as_canonical(db._conn, "session-1")  # noqa: SLF001
    finally:
        db.close()

    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert [e["run_id"] for e in events] == ["run-A", "run-A", "run-B", "run-B"]
    assert [e["type"] for e in events] == [
        "tool.start",
        "tool.complete",
        "tool.start",
        "tool.complete",
    ]


# ---------------------------------------------------------------------------
# session.messages integration — canonical path through the gateway
# ---------------------------------------------------------------------------

def test_session_messages_serves_canonical_tool_events(monkeypatch, tmp_path):
    """``session.messages`` with ``include_tool_events`` returns canonical
    event shapes (real run_events.seq) in the ``toolEvents`` field."""
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event("tool.start", seq=1, run_id="run-1"),
        )
        db.append_run_event(
            "session-1",
            _tool_event("tool.complete", seq=2, run_id="run-1"),
        )

        result = _call_messages(
            "canonical",
            {"session_id": "session-1", "include_tool_events": True},
        )
    finally:
        db.close()

    tool_events = result["toolEvents"]
    assert len(tool_events) == 2
    # Canonical: type + real run_events.seq + run_id.
    assert tool_events[0]["type"] == "tool.start"
    assert tool_events[0]["seq"] == 1
    assert tool_events[0]["run_id"] == "run-1"
    assert tool_events[1]["type"] == "tool.complete"
    assert tool_events[1]["seq"] == 2
    assert tool_events[1]["run_id"] == "run-1"


def test_session_messages_canonical_tool_events_respects_after_seq(monkeypatch, tmp_path):
    """The ``after_seq`` cursor applies to canonical tool events too."""
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.create_session("session-1", "dovie")
        for seq in range(1, 5):
            db.append_run_event(
                "session-1",
                _tool_event(
                    "tool.start",
                    seq=seq,
                    tool_id=f"tool-{seq}",
                    run_id=f"run-{seq}",
                ),
            )

        result = _call_messages(
            "after-seq",
            {
                "session_id": "session-1",
                "include_tool_events": True,
                "after_seq": 2,
            },
        )
    finally:
        db.close()

    seqs = [int(e["seq"]) for e in result["toolEvents"]]
    assert seqs == [3, 4]
    assert all(s > 2 for s in seqs)


def test_session_messages_canonical_tool_events_empty_when_none(monkeypatch, tmp_path):
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.create_session("session-1", "dovie")
        result = _call_messages(
            "empty",
            {"session_id": "session-1", "include_tool_events": True},
        )
    finally:
        db.close()
    assert result["toolEvents"] == []


def test_session_messages_refuses_legacy_tool_event_row_model(monkeypatch, tmp_path):
    db = _install_db(monkeypatch, tmp_path)

    def fail_legacy_tool_events(*args, **kwargs):
        raise AssertionError("legacy db.list_tool_events must not be called")

    monkeypatch.setattr(db, "list_tool_events", fail_legacy_tool_events)
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event("tool.complete", seq=1, run_id="run-1"),
        )

        response = server._methods["session.messages"](
            "no-legacy-row-model",
            {"session_id": "session-1", "include_tool_events": True},
        )
    finally:
        db.close()

    assert "error" not in response, response
    assert [event["seq"] for event in response["result"]["toolEvents"]] == [1]


def test_session_messages_uses_run_event_read_model_for_tool_events(monkeypatch, tmp_path):
    run_event_service = importlib.import_module("tui_gateway.services.run_events")
    db = _install_db(monkeypatch, tmp_path)
    calls: list[tuple[str, int]] = []
    original = run_event_service.RunEventReadModel.list_tool_events

    def traced(self, session_id, *, after_seq=0, limit=2000):
        calls.append((session_id, after_seq))
        return original(self, session_id, after_seq=after_seq, limit=limit)

    monkeypatch.setattr(run_event_service.RunEventReadModel, "list_tool_events", traced)
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event("tool.complete", seq=4, run_id="run-1"),
        )
        response = server._methods["session.messages"](
            "read-model-tool-events",
            {
                "session_id": "session-1",
                "include_tool_events": True,
            },
        )
    finally:
        db.close()

    assert "error" not in response, response
    assert calls == [("session-1", 0)]
    assert [event["seq"] for event in response["result"]["toolEvents"]] == [1]


def test_session_messages_returns_empty_tool_events_without_read_model(monkeypatch):
    """A DB without a SQLite connection cannot serve the P2 read model and must
    not resurrect legacy ``db.list_tool_events`` fallback behavior."""

    class ConnectionlessDB:
        def list_tool_events(self, *args, **kwargs):
            raise AssertionError("legacy db.list_tool_events must not be called")

    session_history = importlib.import_module("tui_gateway.methods.session_history")
    monkeypatch.setattr(session_history, "_get_db", lambda: ConnectionlessDB())

    response = server._methods["session.messages"](
        "legacy-row-model",
        {"session_id": "session-1", "include_tool_events": True},
    )

    assert response["error"]["message"] == "session repository unavailable"


def test_session_messages_has_no_legacy_tool_events_fallback():
    session_history = importlib.import_module("tui_gateway.methods.session_history")
    source = inspect.getsource(session_history)
    assert "list_tool_events_as_canonical" not in source
    assert 'getattr(db, "list_tool_events' not in source
    assert "db.list_tool_events" not in source
    assert "from tui_gateway.services.run_events import list_runtime_events, list_tool_events" in source


def test_session_messages_canonical_tool_events_off_by_default(monkeypatch, tmp_path):
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.create_session("session-1", "dovie")
        db.append_run_event(
            "session-1",
            _tool_event("tool.start", seq=1),
        )
        result = _call_messages(
            "no-flag",
            {"session_id": "session-1"},
        )
    finally:
        db.close()
    assert result["toolEvents"] == []
