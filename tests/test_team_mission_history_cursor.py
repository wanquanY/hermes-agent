"""Cursor semantics tests for team_mission_session_history query path.

Verifies after_seq/before_seq (run_events.seq domain) and after_id/before_id
(messages.id domain) cursor support in
``hermes_team_mission.runtime.history.get_team_mission_node_runtime_history``.

These tests exercise the real SessionDB + the public gateway method
``team_mission.node.history`` end-to-end (real PG-style SQLite), not fakes.
"""
from __future__ import annotations

from pathlib import Path

from tests.team_mission_gateway_test_support import (
    team_mission_gateway,
    team_mission_history_gateway,
)


_ACTIVITY_ID = "act-node:mission-1:node-worker"
_RUNTIME_SCOPE_KEY = "team:mission-1:node:node-worker"


def _setup_db(monkeypatch, tmp_path: Path):
    """Build a real SessionDB wired to the team-mission gateway history method.

    Seeds a mission + node + run binding + session so that
    ``team_mission.node.history`` resolves to ``session-worker``.
    """
    from hermes_state import SessionDB
    from tui_gateway import server

    team_mission = team_mission_gateway()
    team_mission_history = team_mission_history_gateway()
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(team_mission, "_get_db", lambda: db)
    monkeypatch.setattr(team_mission_history, "_get_db", lambda: db)

    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        objective="Build",
        mode="supervised_mission",
        status="running",
        metadata={"conversation_session_id": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        runtime_scope_key=_RUNTIME_SCOPE_KEY,
    )
    db.create_session("team-session-1", source="team_mission")
    db.create_session("session-worker", source="team_mission")
    db.upsert_run(
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key=_RUNTIME_SCOPE_KEY,
        runtime_session_id="runtime-worker",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_session_id="runtime-worker",
        runtime_scope_key=_RUNTIME_SCOPE_KEY,
        role="worker",
    )
    return db, server


def _append_run_event(db, session_id: str, seq: int, event_type: str = "message.delta"):
    """Append a non-terminal run event.

    Using message.delta (not message.complete) avoids the terminal-run dedup
    path in append_run_event: a second message.complete with status=completed
    on an already-terminal run is treated as duplicate_terminal and skipped,
    which would prevent us from seeding seq 1..5 for cursor tests.
    """
    db.append_run_event(
        session_id,
        {
            "type": event_type,
            "stored_session_id": session_id,
            "run_id": "run-worker",
            "turn_id": "turn-worker",
            "runtime_scope_key": _RUNTIME_SCOPE_KEY,
            "seq": seq,
            "activity_id": _ACTIVITY_ID,
            "payload": {"delta": f"event-{seq}"},
        },
    )


def _append_message(db, session_id: str, content: str):
    return db.append_message(
        session_id,
        role="assistant",
        content=content,
        platform_message_id=f"msg-{content}",
        metadata={
            "run_id": "run-worker",
            "turn_id": "turn-worker",
            "activity_id": _ACTIVITY_ID,
            "transcript_activity_kind": "mission_node",
            "node_id": "node-worker",
        },
    )


def test_after_seq_returns_only_new_events(monkeypatch, tmp_path: Path):
    """after_seq=3 returns only events with seq > 3 (i.e. seq 4,5)."""
    db, server = _setup_db(monkeypatch, tmp_path)
    for seq in range(1, 6):
        _append_run_event(db, "session-worker", seq=seq)

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "include_run_events": True,
            "run_events_limit": 100,
            "after_seq": 3,
        },
    )

    assert "error" not in response
    events = response["result"]["run_events"]
    seqs = [event["seq"] for event in events]
    assert seqs == [4, 5], f"expected [4, 5], got {seqs}"


def test_before_seq_returns_earlier_events(monkeypatch, tmp_path: Path):
    """before_seq=3 + limit=2 returns only events with seq < 3 (i.e. seq 1,2)."""
    db, server = _setup_db(monkeypatch, tmp_path)
    for seq in range(1, 6):
        _append_run_event(db, "session-worker", seq=seq)

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "include_run_events": True,
            "run_events_limit": 2,
            "before_seq": 3,
        },
    )

    assert "error" not in response
    events = response["result"]["run_events"]
    seqs = [event["seq"] for event in events]
    assert seqs == [1, 2], f"expected [1, 2], got {seqs}"


def test_after_id_returns_only_new_messages(monkeypatch, tmp_path: Path):
    """after_id=<id of msg 3> returns only messages with id > that (i.e. 4,5)."""
    db, server = _setup_db(monkeypatch, tmp_path)
    message_ids = []
    for i in range(1, 6):
        row_id = _append_message(db, "session-worker", content=f"msg-{i}")
        message_ids.append(row_id)

    after_id = message_ids[2]  # id of the 3rd message
    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "include_run_events": False,
            "limit": 100,
            "after_id": after_id,
        },
    )

    assert "error" not in response
    messages = response["result"]["messages"]
    ids = [message["id"] for message in messages]
    expected = message_ids[3:]
    assert ids == expected, f"expected {expected}, got {ids}"


def test_default_cursor_backward_compatible(monkeypatch, tmp_path: Path):
    """No cursor params = current behaviour: recent N by id DESC, returned ASC."""
    db, server = _setup_db(monkeypatch, tmp_path)
    message_ids = []
    for i in range(1, 6):
        row_id = _append_message(db, "session-worker", content=f"msg-{i}")
        message_ids.append(row_id)

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "include_run_events": False,
            "limit": 3,
        },
    )

    assert "error" not in response
    messages = response["result"]["messages"]
    ids = [message["id"] for message in messages]
    # Default: recent 3 (ids 3,4,5), returned in ASC order
    assert ids == message_ids[2:], f"expected {message_ids[2:]}, got {ids}"


def test_page_info_contains_last_run_event_seq(monkeypatch, tmp_path: Path):
    """page_info has last_run_event_seq = max seq of returned run_events."""
    db, server = _setup_db(monkeypatch, tmp_path)
    for seq in range(1, 6):
        _append_run_event(db, "session-worker", seq=seq)

    response = server._methods["team_mission.node.history"](
        1,
        {
            "mission_id": "mission-1",
            "node_id": "node-worker",
            "include_run_events": True,
            "run_events_limit": 100,
        },
    )

    assert "error" not in response
    page_info = response["result"]["page_info"]
    assert "last_run_event_seq" in page_info
    assert page_info["last_run_event_seq"] == 5
    assert "last_message_id" in page_info
