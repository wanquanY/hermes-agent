"""Run-event maintenance orchestration contracts."""

from __future__ import annotations

import json

from hermes_agent.storage.cli_session_store import open_cli_session_store


def _insert_session_info(store, *, seq: int, model: str) -> None:
    payload = {"model": model, "provider": "test"}
    event = {
        "type": "session.info",
        "session_id": "runtime-1",
        "conversation_session_id": "session-1",
        "execution_session_id": "runtime-1",
        "runtime_scope_key": "profile:agent-default",
        "run_id": "run-1",
        "turn_id": "turn-1",
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
        """,
        (
            "session-1",
            "run-1",
            "turn-1",
            "runtime-1",
            "profile:agent-default",
            "session.info",
            seq,
            float(seq),
            json.dumps(payload, separators=(",", ":")),
            json.dumps(event, separators=(",", ":")),
        ),
    )


def test_duplicate_session_info_pruning_keeps_the_latest_snapshot(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create("session-1", source="test")
        _insert_session_info(store, seq=1, model="same-model")
        _insert_session_info(store, seq=2, model="same-model")

        result = store.run_event_maintenance.prune_duplicate_session_info(
            session_id="session-1"
        )

        events = store.runs.list_events("session-1")
        archive = store._conn.execute(  # noqa: SLF001 - storage contract assertion.
            "SELECT reason, event_count, first_seq, last_seq FROM run_event_archives"
        ).fetchone()
        assert result == {"deleted_events": 1}
        assert [event["seq"] for event in events] == [2]
        assert archive is not None
        assert dict(archive) == {
            "reason": "duplicate_session_info",
            "event_count": 1,
            "first_seq": 1,
            "last_seq": 1,
        }
    finally:
        store.close()


def test_duplicate_session_info_pruning_preserves_changed_snapshots(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create("session-1", source="test")
        _insert_session_info(store, seq=1, model="first-model")
        _insert_session_info(store, seq=2, model="next-model")

        result = store.run_event_maintenance.prune_duplicate_session_info(
            session_id="session-1"
        )

        assert result == {"deleted_events": 0}
        assert [event["seq"] for event in store.runs.list_events("session-1")] == [1, 2]
    finally:
        store.close()


def test_auto_compaction_persists_its_interval_marker(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        first = store.run_event_maintenance.maybe_auto_compact(vacuum=False)
        second = store.run_event_maintenance.maybe_auto_compact(vacuum=False)

        assert first["skipped"] is False
        assert first["vacuumed"] is False
        assert second == {"skipped": True, "reason": "interval"}
        assert store.metadata.get("last_auto_run_event_compaction_v1")
    finally:
        store.close()
