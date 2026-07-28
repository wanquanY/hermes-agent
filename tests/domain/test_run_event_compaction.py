"""Behavioral contracts for canonical run-event compaction."""

from __future__ import annotations

import json
from typing import Any

from hermes_agent.composition.cli_session_store import open_cli_session_store


def _insert_event(
    store,
    *,
    seq: int,
    event_type: str,
    payload: dict[str, Any],
    run_id: str = "run-1",
    status: str = "",
) -> None:
    event = {
        "type": event_type,
        "session_id": "runtime-1",
        "conversation_session_id": "session-1",
        "run_id": run_id,
        "turn_id": "turn-1",
        "runtime_scope_key": "profile:agent-default",
        "seq": seq,
        "timestamp": float(seq),
        "payload": payload,
    }
    store._conn.execute(  # noqa: SLF001 - storage contract setup.
        """
        INSERT INTO run_events (
            session_id, run_id, turn_id, execution_session_id,
            runtime_scope_key, event_type, seq, timestamp,
            payload_json, event_json, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "session-1",
            run_id,
            "turn-1",
            "runtime-1",
            "profile:agent-default",
            event_type,
            seq,
            float(seq),
            json.dumps(payload, separators=(",", ":")),
            json.dumps(event, separators=(",", ":")),
            status,
        ),
    )


def test_compaction_prunes_terminal_streams_and_preserves_tool_facts(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.runs.upsert(
            run_id="run-1",
            session_id="session-1",
            status="completed",
            runtime_scope_key="profile:agent-default",
        )
        _insert_event(
            store,
            seq=1,
            event_type="tool.complete",
            payload={"tool_name": "terminal", "result": "durable output"},
        )
        _insert_event(
            store,
            seq=2,
            event_type="message.delta",
            payload={"mode": "append", "text": "draft"},
        )
        _insert_event(
            store,
            seq=3,
            event_type="message.complete",
            payload={"text": "final"},
            status="completed",
        )

        result = store.run_event_maintenance.compact(session_id="session-1")

        events = store.runs.list_events("session-1")
        archive = store._conn.execute(  # noqa: SLF001 - storage contract assertion.
            "SELECT reason, event_count, first_seq, last_seq FROM run_event_archives"
        ).fetchone()
        assert result["pruned_terminal_stream_events"] == 1
        assert result["deleted_events"] == 1
        assert [event["type"] for event in events] == [
            "tool.complete",
            "message.complete",
        ]
        assert archive is not None
        assert dict(archive) == {
            "reason": "terminal_run_stream_events",
            "event_count": 1,
            "first_seq": 2,
            "last_seq": 2,
        }
    finally:
        store.close()


def test_compaction_prunes_every_terminal_stream_class(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.runs.upsert(
            run_id="run-1",
            session_id="session-1",
            status="completed",
            runtime_scope_key="profile:agent-default",
        )
        event_types = (
            "message.delta",
            "reasoning.delta",
            "thinking.delta",
            "subagent.output_delta",
            "subagent.reasoning_delta",
            "subagent.thinking",
            "agent_profile_test.output_delta",
            "agent_profile_test.thinking",
            "tool.progress",
            "tool.generating",
        )
        for seq, event_type in enumerate(event_types, start=1):
            _insert_event(
                store,
                seq=seq,
                event_type=event_type,
                payload={"mode": "append", "delta": event_type},
            )
        _insert_event(
            store,
            seq=len(event_types) + 1,
            event_type="message.complete",
            payload={"text": "final"},
            status="completed",
        )

        result = store.run_event_maintenance.compact(session_id="session-1")

        assert result["pruned_terminal_stream_events"] == len(event_types)
        assert [event["type"] for event in store.runs.list_events("session-1")] == [
            "message.complete"
        ]
    finally:
        store.close()


def test_compaction_detects_terminal_streams_without_a_materialized_run(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create("session-1", source="test")
        _insert_event(
            store,
            seq=1,
            event_type="message.delta",
            payload={"mode": "append", "text": "draft"},
            run_id="missing-run",
        )
        _insert_event(
            store,
            seq=2,
            event_type="message.complete",
            payload={"text": "final"},
            run_id="missing-run",
            status="completed",
        )

        result = store.run_event_maintenance.compact(session_id="session-1")

        assert result["pruned_terminal_stream_events"] == 1
        assert [event["type"] for event in store.runs.list_events("session-1")] == [
            "message.complete"
        ]
        assert store.runs.get("missing-run") is None
    finally:
        store.close()


def test_compaction_never_rewrites_active_run_streams(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.runs.upsert(
            run_id="run-1",
            session_id="session-1",
            status="running",
            runtime_scope_key="profile:agent-default",
        )
        for seq, text in ((1, "A"), (2, "B")):
            _insert_event(
                store,
                seq=seq,
                event_type="reasoning.delta",
                payload={"mode": "append", "delta": text},
            )

        result = store.run_event_maintenance.compact(session_id="session-1")

        assert result["deleted_events"] == 0
        assert [event["seq"] for event in store.runs.list_events("session-1")] == [1, 2]
    finally:
        store.close()


def test_compaction_coalesces_nonterminal_stream_segments(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create("session-1", source="test")
        for seq, text in ((1, "reason"), (2, "ing")):
            _insert_event(
                store,
                seq=seq,
                event_type="reasoning.delta",
                payload={"mode": "append", "delta": text},
                run_id="run-without-state",
            )

        result = store.run_event_maintenance.compact(session_id="session-1")

        events = store.runs.list_events("session-1")
        assert result["compacted_segments"] == 1
        assert result["deleted_events"] == 1
        assert len(events) == 1
        assert events[0]["seq"] == 2
        assert events[0]["payload"]["mode"] == "append"
        assert events[0]["payload"]["delta"] == "reasoning"
    finally:
        store.close()


def test_compaction_deduplicates_terminal_frames_at_the_canonical_seq(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.runs.upsert(
            run_id="run-1",
            session_id="session-1",
            status="completed",
            runtime_scope_key="profile:agent-default",
        )
        _insert_event(
            store,
            seq=11,
            event_type="message.complete",
            payload={"text": "first"},
            status="completed",
        )
        _insert_event(
            store,
            seq=12,
            event_type="message.complete",
            payload={"text": "latest"},
            status="completed",
        )

        result = store.run_event_maintenance.compact(session_id="session-1")

        events = store.runs.list_events("session-1")
        run = store.runs.get("run-1")
        assert result["deduplicated_terminal_groups"] == 1
        assert result["deleted_events"] == 1
        assert [(event["seq"], event["payload"]["text"]) for event in events] == [
            (11, "latest")
        ]
        assert run is not None
        assert run["last_seq"] == 11
    finally:
        store.close()
