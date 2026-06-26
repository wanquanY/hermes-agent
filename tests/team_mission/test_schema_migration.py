from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_state import SessionDB


TEAM_MISSION_TABLES = {
    "team_mission_conversations",
    "team_missions",
    "team_mission_nodes",
    "team_mission_edges",
    "team_mission_run_bindings",
    "team_mission_events",
    "team_mission_artifacts",
    "team_mission_deliverables",
    "team_mission_memory_items",
    "team_mission_memory_edges",
}


TEAM_MISSION_INDEXES = {
    "idx_team_mission_conversations_team",
    "idx_team_mission_conversations_workspace",
    "idx_team_missions_conversation",
    "idx_team_missions_status_updated",
    "idx_team_mission_nodes_mission",
    "idx_team_mission_edges_mission",
    "idx_team_mission_run_bindings_mission",
    "idx_team_mission_run_bindings_session",
    "idx_team_mission_events_mission_seq",
    "idx_team_mission_events_source_run",
    "idx_team_mission_artifacts_mission",
    "idx_team_mission_deliverables_mission",
    "idx_team_mission_deliverables_run",
    "idx_team_mission_memory_items_mission",
    "idx_team_mission_memory_items_conversation",
    "idx_team_mission_memory_items_team",
    "idx_team_mission_memory_edges_from",
    "idx_team_mission_memory_edges_to",
}


def _names(db: SessionDB, *, object_type: str) -> set[str]:
    rows = db._conn.execute(  # noqa: SLF001 - schema contract assertion.
        "SELECT name FROM sqlite_master WHERE type = ?",
        (object_type,),
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _team_mission_node_pk(db: SessionDB) -> list[str]:
    rows = db._conn.execute(  # noqa: SLF001 - schema contract assertion.
        'PRAGMA table_info("team_mission_nodes")'
    ).fetchall()
    return [
        str(row["name"])
        for row in sorted(rows, key=lambda item: int(item["pk"] or 0))
        if int(row["pk"] or 0)
    ]


def test_team_mission_schema_is_created_for_empty_database(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    assert TEAM_MISSION_TABLES <= _names(db, object_type="table")
    assert TEAM_MISSION_INDEXES <= _names(db, object_type="index")
    assert _team_mission_node_pk(db) == ["mission_id", "node_id"]


def test_team_mission_schema_migrates_legacy_global_node_primary_key(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (1);

        CREATE TABLE team_missions (
            mission_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE team_mission_nodes (
            node_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        INSERT INTO team_missions (
            mission_id, title, mode, status, created_at, updated_at
        ) VALUES (
            'mission-legacy', 'Legacy mission', 'supervised_mission', 'running', 1, 1
        );

        INSERT INTO team_mission_nodes (
            node_id, mission_id, kind, title, status, created_at, updated_at
        ) VALUES (
            'worker', 'mission-legacy', 'worker', 'Worker', 'ready', 2, 3
        );
        """
    )
    conn.commit()
    conn.close()

    db = SessionDB(db_path)
    row = db._conn.execute(  # noqa: SLF001 - schema migration assertion.
        """
        SELECT mission_id, node_id, kind, title, status, position_x, position_y
        FROM team_mission_nodes
        WHERE mission_id = ? AND node_id = ?
        """,
        ("mission-legacy", "worker"),
    ).fetchone()

    assert _team_mission_node_pk(db) == ["mission_id", "node_id"]
    assert row is not None
    assert row["kind"] == "worker"
    assert row["title"] == "Worker"
    assert row["status"] == "ready"
    assert row["position_x"] == 0
    assert row["position_y"] == 0
    assert "idx_team_mission_nodes_mission" in _names(db, object_type="index")
