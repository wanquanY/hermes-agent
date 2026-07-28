from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.domain import seq_allocator
from hermes_agent.domain.exceptions import SeqAllocatorBusy
from hermes_agent.domain.seq_allocator import allocate_only
from hermes_agent.composition.migrations import CURRENT_SCHEMA_VERSION
from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services import run_control


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _event(*, seq: int = 0, run_id: str = "", index: int = 0) -> dict[str, Any]:
    return {
        "type": "debug.trace",
        "seq": seq,
        "run_id": run_id,
        "turn_id": f"turn-{index}" if index else "",
        "timestamp": 1000.0 + index,
        "payload": {"index": index},
    }


def _create_legacy_v43_db(path: Path) -> None:
    conn = _connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (43);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                started_at REAL
            );
            INSERT INTO sessions (id, source, started_at)
            VALUES ('s-with-events', 'test', 1), ('s-empty', 'test', 1);

            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT,
                event_type TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                event_json TEXT NOT NULL,
                UNIQUE(session_id, seq)
            );
            INSERT INTO run_events (session_id, run_id, event_type, seq, timestamp, event_json)
            VALUES
                ('s-with-events', 'run-1', 'debug.trace', 1, 1, '{"type":"debug.trace","seq":1}'),
                ('s-with-events', 'run-2', 'debug.trace', 5, 5, '{"type":"debug.trace","seq":5}');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_seq_counter_migration_backfills_existing_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_legacy_v43_db(db_path)

    db = open_cli_session_store(db_path)
    try:
        rows = {
            str(row["session_id"]): int(row["next_seq"])
            for row in db._conn.execute("SELECT session_id, next_seq FROM seq_counter")
        }
        version = int(db._conn.execute("SELECT version FROM schema_version").fetchone()[0])
    finally:
        db.close()

    assert version == CURRENT_SCHEMA_VERSION
    assert rows["s-with-events"] == 6
    assert rows["s-empty"] == 1


def test_append_run_event_uses_allocator_not_inbound_runtime_seq(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("s1", source="test")

        first = db.runs.append_event("s1", _event(seq=100, run_id="run-1", index=1))
        second = db.runs.append_event("s1", _event(seq=500, run_id="run-2", index=2))

        rows = db._conn.execute(
            """
            SELECT seq, runtime_source_seq, event_json
              FROM run_events
             WHERE session_id = ?
             ORDER BY seq
            """,
            ("s1",),
        ).fetchall()
        next_seq = db.runs.next_event_seq("s1")
        counter = db._conn.execute(
            "SELECT next_seq FROM seq_counter WHERE session_id = ?",
            ("s1",),
        ).fetchone()
    finally:
        db.close()

    assert [first["seq"], second["seq"]] == [1, 2]
    assert [int(row["seq"]) for row in rows] == [1, 2]
    assert [int(row["runtime_source_seq"]) for row in rows] == [100, 500]
    assert '"seq":1' in str(rows[0]["event_json"])
    assert '"runtime_source_seq":100' in str(rows[0]["event_json"])
    assert next_seq == 3
    assert int(counter["next_seq"]) == 3


def test_next_run_event_seq_does_not_fallback_to_run_events_max(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("s-missing-counter", source="test")
        db.runs.append_event("s-missing-counter", _event(seq=100, run_id="run-1", index=1))
        db._conn.execute("DELETE FROM seq_counter WHERE session_id = ?", ("s-missing-counter",))

        assert db.runs.next_event_seq("s-missing-counter") == 0
        assert db.runs.next_event_seq("s-missing-counter", fallback_seq=99) == 99
    finally:
        db.close()


def test_append_run_event_allocates_unique_seq_under_parallel_writes(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    total_workers = 12
    per_worker = 15
    total_events = total_workers * per_worker

    try:
        db.sessions.create("s-parallel", source="test")

        def append_one(index: int) -> int:
            saved = db.runs.append_event(
                "s-parallel",
                _event(seq=10_000 + index, run_id=f"run-{index}", index=index),
            )
            return int(saved["seq"])

        with ThreadPoolExecutor(max_workers=total_workers) as pool:
            assigned = list(pool.map(append_one, range(1, total_events + 1)))

        rows = db._conn.execute(
            "SELECT seq FROM run_events WHERE session_id = ? ORDER BY seq",
            ("s-parallel",),
        ).fetchall()
        stored = [int(row["seq"]) for row in rows]
        next_seq = db.runs.next_event_seq("s-parallel")
    finally:
        db.close()

    assert sorted(assigned) == list(range(1, total_events + 1))
    assert stored == list(range(1, total_events + 1))
    assert next_seq == total_events + 1


def test_allocate_only_shares_session_seq_domain_with_run_events(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("s-activity", source="test")

        first_activity_seq = db._execute_write(
            lambda conn: allocate_only(conn, session_id="s-activity", updated_at=1000)
        )
        run_event = db.runs.append_event("s-activity", _event(seq=9000, run_id="run-1", index=1))
        second_activity_seq = db._execute_write(
            lambda conn: allocate_only(conn, session_id="s-activity", updated_at=1002)
        )
        next_seq = db.runs.next_event_seq("s-activity")
    finally:
        db.close()

    assert first_activity_seq == 1
    assert run_event["seq"] == 2
    assert second_activity_seq == 3
    assert next_seq == 4


def test_seq_allocator_busy_raises_typed_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("s-busy", source="test")

        def always_busy(*_args: Any, **_kwargs: Any) -> int:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(seq_allocator, "_BUSY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
        monkeypatch.setattr(seq_allocator, "_allocate_once", always_busy)

        with pytest.raises(SeqAllocatorBusy):
            db._execute_write(lambda conn: allocate_only(conn, session_id="s-busy", updated_at=1))
    finally:
        db.close()


def test_session_db_sets_sqlite_busy_timeout(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        timeout = int(db._conn.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        db.close()

    assert timeout == 5000


def test_publish_run_terminal_event_does_not_preassign_runtime_seq(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("s-terminal", source="test")
        db.runs.upsert(
            run_id="run-terminal",
            session_id="s-terminal",
            runtime_scope_key="s-terminal",
            turn_id="turn-terminal",
            execution_session_id="runtime-terminal",
            status="running",
        )

        published = run_control.publish_run_terminal_event(
            conversation_session_id="s-terminal",
            run_id="run-terminal",
            turn_id="turn-terminal",
            runtime_scope_key="s-terminal",
            execution_session_id="runtime-terminal",
            status="failed",
            message="worker crashed",
            db=db,
        )
        row = db._conn.execute(
            """
            SELECT seq, runtime_source_seq, event_json
              FROM run_events
             WHERE session_id = ?
             ORDER BY seq DESC
             LIMIT 1
            """,
            ("s-terminal",),
        ).fetchone()
    finally:
        db.close()

    # Phase C spec §7.2 — RunStateMachine.terminate_run atomically allocates
    # terminal_seq via SeqAllocator BEFORE publishing the notification frame.
    # The published frame carries that seq so downstream subscribers can
    # align with the persisted canonical event.
    assert "seq" in published and int(published["seq"]) == 1
    assert int(row["seq"]) == 1
    # runtime_source_seq remains 0 — canonical seq is authoritative; the
    # legacy runtime_source_seq mirror is deprecated (Phase M drop target).
    assert int(row["runtime_source_seq"]) == 0
    assert '"seq":1' in str(row["event_json"])
