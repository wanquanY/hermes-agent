from __future__ import annotations

import sqlite3

from hermes_agent.application.team_mission_audit_log import TeamMissionAuditLog


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE team_missions (
            mission_id TEXT PRIMARY KEY
        );
        CREATE TABLE team_mission_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            source_event_type TEXT,
            source_run_id TEXT,
            source_session_id TEXT,
            source_seq INTEGER,
            dedupe_key TEXT NOT NULL,
            timestamp REAL,
            payload_json TEXT,
            source_event_json TEXT,
            event_json TEXT NOT NULL,
            created_at REAL,
            UNIQUE(mission_id, dedupe_key),
            UNIQUE(mission_id, seq)
        );
        CREATE TABLE team_mission_event_seq_counter (
            mission_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
            updated_at REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO team_missions (mission_id) VALUES ('mission-1')")
    return conn


def test_append_dedupes_and_patches_top_level_and_payload_seq():
    conn = _make_conn()
    audit_log = TeamMissionAuditLog(conn)

    first = audit_log.append(
        mission_id="mission-1",
        dedupe_key="event-1",
        event={"type": "team_mission.runtime.event", "payload": {"kind": "node.message"}},
        event_type="team_mission.runtime.event",
        source_event_type="message.complete",
        source_run_id="run-1",
        source_session_id="session-1",
        source_seq=7,
        timestamp=10,
        now=11,
    )
    duplicate = audit_log.append(
        mission_id="mission-1",
        dedupe_key="event-1",
        event={"type": "team_mission.runtime.event", "payload": {"kind": "node.message"}},
    )

    assert first.inserted is True
    assert first.event["seq"] == 1
    assert first.event["team_mission_event_seq"] == 1
    assert first.event["teamMissionEventSeq"] == 1
    assert first.event["payload"]["seq"] == 1
    assert first.event["payload"]["team_mission_event_seq"] == 1
    assert first.event["payload"]["teamMissionEventSeq"] == 1
    assert duplicate.inserted is False
    assert duplicate.event["_persistence_disposition"] == "duplicate_mission_event"
    assert conn.execute("SELECT COUNT(*) AS count FROM team_mission_events").fetchone()["count"] == 1


def test_list_latest_and_prune_source_event_types():
    conn = _make_conn()
    audit_log = TeamMissionAuditLog(conn)
    for key, source_type in (
        ("event-1", "message.delta"),
        ("event-2", "message.complete"),
        ("event-3", "reasoning.delta"),
    ):
        audit_log.append(
            mission_id="mission-1",
            dedupe_key=key,
            event={"type": "team_mission.runtime.event", "payload": {"source_event_type": source_type}},
            event_type="team_mission.runtime.event",
            source_event_type=source_type,
        )

    assert [event["seq"] for event in audit_log.list("mission-1")] == [1, 2, 3]
    assert [event["seq"] for event in audit_log.list("mission-1", after_seq=1)] == [2, 3]
    assert audit_log.latest_seq("mission-1") == 3

    deleted = audit_log.prune_source_event_types(
        mission_id="mission-1",
        source_event_types=("message.delta", "reasoning.delta"),
    )

    assert deleted == 2
    assert [event["seq"] for event in audit_log.list("mission-1")] == [2]
