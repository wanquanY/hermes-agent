"""PR-2 §4.2: query cursor tests for ``session.messages`` and ``session.events``.

These tests exercise the run_events-dimension cursor (``after_seq`` /
``before_seq`` / ``maxSeq``) added to ``session.messages`` and the new
lightweight ``session.events`` reader.  They construct a real ``CliSessionStore``
against ``tmp_path``, seed run_events via ``append_run_event`` (which
auto-assigns authoritative ``seq`` values), and invoke the JSON-RPC handlers
through ``server._methods`` exactly as the gateway dispatcher does.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway import server


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _install_db(monkeypatch: Any, tmp_path: Path) -> CliSessionStore:
    """Build a real CliSessionStore and route ``_get_db`` in session_history to it."""
    session_history = importlib.import_module("tui_gateway.methods.session_history")
    db = open_cli_session_store(tmp_path / "state.db")
    monkeypatch.setattr(session_history, "_get_db", lambda: db)
    return db


def _frame(
    *,
    run_id: str,
    text: str,
    conversation_session_id: str = "sess-1",
    seq: int = 0,
) -> dict[str, Any]:
    """A minimal message.complete frame suitable for ``append_run_event``."""
    return {
        "type": "message.complete",
        "session_id": f"runtime-{run_id}",
        "conversation_session_id": conversation_session_id,
        "run_id": run_id,
        "turn_id": f"turn-{run_id}",
        "seq": seq,
        "payload": {"text": text, "status": "complete"},
    }


def _seed_events(db: CliSessionStore, conversation_session_id: str = "sess-1", n: int = 5) -> list[int]:
    """Append ``n`` run_events; return the list of assigned seq values."""
    seqs: list[int] = []
    for i in range(n):
        db.runs.append_event(
            conversation_session_id,
            _frame(run_id=f"run-{i + 1}", text=f"event-{i + 1}"),
        )
    for event in db.runs.list_events(conversation_session_id):
        seqs.append(int(event["seq"]))
    return seqs


def _call_messages(rid: str, params: dict[str, Any]) -> dict[str, Any]:
    response = server._methods["session.messages"](rid, params)
    assert "error" not in response, response
    return response["result"]


def _call_events(rid: str, params: dict[str, Any]) -> dict[str, Any]:
    response = server._methods["session.events"](rid, params)
    assert "error" not in response, response
    return response["result"]


# ---------------------------------------------------------------------------
# session.messages cursor
# ---------------------------------------------------------------------------

def test_session_messages_after_seq_filters_to_seq_greater_than_cursor(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        seqs = _seed_events(db)
        assert seqs == [1, 2, 3, 4, 5]

        result = _call_messages(
            "after-seq",
            {"session_id": "sess-1", "include_run_events": True, "after_seq": 2},
        )

        returned_seqs = [int(e["seq"]) for e in result["runEvents"]]
        assert returned_seqs == [3, 4, 5]
        assert all(seq > 2 for seq in returned_seqs)
    finally:
        db.close()


def test_session_messages_before_seq_filters_to_seq_less_than_cursor(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db)

        result = _call_messages(
            "before-seq",
            {"session_id": "sess-1", "include_run_events": True, "before_seq": 4},
        )

        returned_seqs = [int(e["seq"]) for e in result["runEvents"]]
        assert returned_seqs == [1, 2, 3]
        assert all(seq < 4 for seq in returned_seqs)
    finally:
        db.close()


def test_session_messages_before_seq_with_after_seq_window(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """after_seq + before_seq together define a half-open (after, before) window."""
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db)

        result = _call_messages(
            "window",
            {
                "session_id": "sess-1",
                "include_run_events": True,
                "after_seq": 1,
                "before_seq": 4,
            },
        )

        returned_seqs = [int(e["seq"]) for e in result["runEvents"]]
        assert returned_seqs == [2, 3]
    finally:
        db.close()


def test_session_messages_response_includes_max_seq(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db)

        result = _call_messages(
            "max-seq",
            {"session_id": "sess-1", "include_run_events": True, "after_seq": 1},
        )

        assert result["maxSeq"] == 5
    finally:
        db.close()


def test_session_messages_max_seq_zero_when_no_run_events(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")

        result = _call_messages(
            "max-seq-empty",
            {"session_id": "sess-1", "include_run_events": True},
        )

        assert result["runEvents"] == []
        assert result["maxSeq"] == 0
    finally:
        db.close()


def test_session_messages_legacy_full_window_emits_deprecation_warning(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db)

        # No after_seq / before_seq → legacy full window + warning.
        result = _call_messages(
            "legacy",
            {"session_id": "sess-1", "include_run_events": True},
        )

        assert len(result["runEvents"]) == 5
        assert "runEventsWarning" in result
        assert "deprecated" in result["runEventsWarning"]
    finally:
        db.close()


def test_session_messages_cursor_window_has_no_deprecation_warning(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db)

        result = _call_messages(
            "cursor-no-warn",
            {"session_id": "sess-1", "include_run_events": True, "after_seq": 1},
        )

        assert "runEventsWarning" not in result
    finally:
        db.close()


# ---------------------------------------------------------------------------
# session.events
# ---------------------------------------------------------------------------

def test_session_events_returns_events_max_seq_and_has_more(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db)

        result = _call_events("events-basic", {"session_id": "sess-1"})

        assert result["session_id"] == "sess-1"
        assert len(result["events"]) == 5
        assert result["maxSeq"] == 5
        # 5 events < default limit(200) → no more.
        assert result["hasMore"] is False
    finally:
        db.close()


def test_session_events_after_seq_filters(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db)

        result = _call_events(
            "events-after",
            {"session_id": "sess-1", "after_seq": 3},
        )

        returned_seqs = [int(e["seq"]) for e in result["events"]]
        assert returned_seqs == [4, 5]
        assert result["maxSeq"] == 5
    finally:
        db.close()


def test_session_events_limit_caps_result_and_sets_has_more(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db, n=5)

        result = _call_events(
            "events-limit",
            {"session_id": "sess-1", "limit": 3},
        )

        assert len(result["events"]) == 3
        returned_seqs = [int(e["seq"]) for e in result["events"]]
        assert returned_seqs == [1, 2, 3]
        assert result["hasMore"] is True
    finally:
        db.close()


def test_session_events_limit_then_after_seq_paginates(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Cursor chain: page 1 (limit 3) → page 2 (after_seq = maxSeq of page 1)."""
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")
        _seed_events(db, n=5)

        page1 = _call_events("events-page1", {"session_id": "sess-1", "limit": 3})
        assert len(page1["events"]) == 3
        assert page1["hasMore"] is True
        cursor = page1["maxSeq"]

        page2 = _call_events(
            "events-page2",
            {"session_id": "sess-1", "limit": 3, "after_seq": cursor},
        )
        assert len(page2["events"]) == 2
        returned_seqs = [int(e["seq"]) for e in page2["events"]]
        assert returned_seqs == [4, 5]
        assert page2["hasMore"] is False
    finally:
        db.close()


def test_session_events_empty_session_returns_empty(
    monkeypatch: Any, tmp_path: Path
) -> None:
    db = _install_db(monkeypatch, tmp_path)
    try:
        db.sessions.create("sess-1", source="test")

        result = _call_events("events-empty", {"session_id": "sess-1"})

        assert result["events"] == []
        assert result["maxSeq"] == 0
        assert result["hasMore"] is False
    finally:
        db.close()


def test_session_events_missing_session_id_is_error(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_db(monkeypatch, tmp_path)
    response = server._methods["session.events"]("no-sid", {})
    assert "error" in response
    assert response["error"]["code"] == 4006


def test_session_events_unknown_session_is_error(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_db(monkeypatch, tmp_path)
    response = server._methods["session.events"](
        "unknown-sess", {"session_id": "does-not-exist"}
    )
    assert "error" in response
    assert response["error"]["code"] == 4007
