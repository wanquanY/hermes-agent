from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

from hermes_agent.composition.cli_session_store import open_cli_session_store


def _migration_module(version: int = 48):
    suffix = (
        "conversation_memory_and_actor_context.py"
        if version == 48
        else "retire_team_mission_memory_tables.py"
    )
    path = Path(__file__).parents[2] / f"hermes_agent/composition/migrations/{version:04d}_{suffix}"
    spec = importlib.util.spec_from_file_location(f"migration_{version:04d}_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE conversation_participants (
            conversation_session_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (conversation_session_id, participant_id)
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT,
            metadata_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE team_mission_memory_items (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            conversation_session_id TEXT NOT NULL,
            task_id TEXT,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            structured_payload_json TEXT,
            source_node_ids_json TEXT,
            source_run_ids_json TEXT,
            artifact_refs_json TEXT,
            workspace_refs_json TEXT,
            confidence REAL NOT NULL,
            visibility TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            invalidated_at REAL
        );
        """
    )
    conn.execute("INSERT INTO sessions VALUES ('conversation-1')")
    conn.execute(
        "INSERT INTO conversation_participants (conversation_session_id, participant_id) VALUES (?, ?)",
        ("conversation-1", "member:a"),
    )
    return conn


def test_migration_backfills_memory_and_repairs_role_pollution() -> None:
    conn = _legacy_db()
    conn.execute(
        """
        INSERT INTO team_mission_memory_items VALUES (
            'legacy-1', 'team-1', 'mission-1', 'conversation-1', 'task-1',
            'mission_task', 'summary', 'Verified result', '{}', '["node-1"]',
            '["run-1"]', '[]', '[]', 0.9, 'team', 'committed', 1, 2, NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO messages (role, content, metadata_json) VALUES ('user', ?, '{}')",
        (
            "You are the Team Leader for a Dovie team conversation.\n\n"
            "Internal policy\n\nUser message:\n真实用户消息",
        ),
    )
    conn.execute(
        "INSERT INTO messages (role, content, metadata_json) VALUES ('user', ?, '{}')",
        ("You are the Team Leader in a Dovie team conversation.\nOnly policy",),
    )

    module = _migration_module()
    module.apply(conn.cursor())

    memory = conn.execute(
        "SELECT * FROM conversation_memory_items WHERE memory_id = 'conversation-memory:legacy-1'"
    ).fetchone()
    assert memory is not None
    assert memory[2:4] == ("activity", "mission:mission-1")
    messages = conn.execute(
        "SELECT content, active, metadata_json FROM messages ORDER BY id"
    ).fetchall()
    assert messages[0][0] == "真实用户消息"
    assert messages[0][1] == 1
    assert messages[1][1] == 0
    assert json.loads(messages[0][2])["role_contract_migration"]["version"] == 48


def test_retirement_migration_keeps_canonical_backfill_and_drops_legacy_source() -> None:
    conn = _legacy_db()
    conn.execute(
        """
        INSERT INTO team_mission_memory_items VALUES (
            'legacy-1', 'team-1', 'mission-1', 'conversation-1', 'task-1',
            'mission_task', 'summary', 'Verified result', '{}', '[]',
            '[]', '[]', '[]', 0.9, 'team', 'committed', 1, 2, NULL
        )
        """
    )

    _migration_module(48).apply(conn.cursor())
    _migration_module(49).apply(conn.cursor())

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "team_mission_memory_items" not in tables
    assert "team_mission_memory_edges" not in tables
    canonical = conn.execute(
        "SELECT content, status FROM conversation_memory_items "
        "WHERE memory_id = 'conversation-memory:legacy-1'"
    ).fetchone()
    assert canonical == ("Verified result", "committed")


def test_mirror_retirement_backfills_deliverable_then_removes_duplicate_events(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    db = open_cli_session_store(db_path)
    db.sessions.create("team-session-1", source="team_mission", transient=False)
    db.sessions.create("synthesis-session-1", source="team_mission", transient=False)
    db.upsert_team_mission(
        mission_id="mission-1",
        title="Mission",
        mode="autonomous_mission",
        metadata={"conversationTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-synthesis",
        kind="synthesis",
        title="Synthesis",
        status="completed",
    )
    db.runs.append_event(
        "synthesis-session-1",
        {
            "type": "message.delta",
            "run_id": "run-synthesis",
            "seq": 1,
            "payload": {"delta": "Canonical final report", "mode": "append"},
        },
    )
    db.runs.append_event(
        "team-session-1",
        {
            "type": "message.complete",
            "run_id": "team-mission:mission-1:conversation:run-synthesis",
            "seq": 1,
            "payload": {
                "status": "complete",
                "mission_id": "mission-1",
                "node_id": "node-synthesis",
                "source_run_id": "run-synthesis",
                "source_session_id": "synthesis-session-1",
                "team_mission_conversation_mirror": True,
                "team_mission_final_deliverable": True,
            },
        },
    )
    db._conn.execute("DELETE FROM applied_migrations WHERE version = 50")
    db._conn.execute("UPDATE schema_version SET version = 49")
    db._conn.commit()
    db.close()

    migrated = open_cli_session_store(db_path)
    deliverable = migrated.latest_team_mission_deliverable_for_run("run-synthesis")
    assert deliverable["source"] == "legacy_imported"
    assert deliverable["summary"] == "Canonical final report"
    assert migrated.runs.list_events("team-session-1") == []
    migrated.close()
