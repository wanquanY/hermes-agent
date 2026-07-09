from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_state import SessionDB
from hermes_team_mission.state.store import open_team_mission_state_store


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


def test_team_mission_schema_preserves_canonical_execution_session_columns(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE team_missions (
            mission_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            team_id TEXT,
            title TEXT NOT NULL,
            objective TEXT,
            workspace_id TEXT,
            workspace_path TEXT,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            leader_session_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            metadata_json TEXT
        );

        CREATE TABLE team_mission_nodes (
            node_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            objective TEXT,
            status TEXT NOT NULL,
            assignee_profile_id TEXT,
            assignee_profile_version_id TEXT,
            canonical_node_id TEXT,
            task_frame_id TEXT,
            runtime_conversation_session_id TEXT,
            execution_session_id TEXT,
            runtime_scope_key TEXT,
            output_contract_json TEXT,
            metadata_json TEXT,
            position_x REAL NOT NULL DEFAULT 0,
            position_y REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (mission_id, node_id)
        );

        CREATE TABLE team_mission_run_bindings (
            run_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL,
            node_id TEXT,
            session_id TEXT NOT NULL,
                execution_session_id TEXT,
            runtime_scope_key TEXT,
            role TEXT NOT NULL,
            metadata_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        INSERT INTO team_missions (
            mission_id, conversation_id, team_id, title, objective, workspace_id,
            workspace_path, mode, status, leader_session_id, created_at, updated_at,
            completed_at, metadata_json
        ) VALUES (
            'mission-execution', 'conversation-execution', 'team-1',
            'Execution mission', 'Render canonical execution columns', 'workspace-1',
            '/tmp/workspace', 'supervised_mission', 'running', 'leader-session',
            1, 2, NULL, '{}'
        );

        INSERT INTO team_mission_nodes (
            node_id, mission_id, kind, title, objective, status,
            assignee_profile_id, assignee_profile_version_id, canonical_node_id,
            task_frame_id, runtime_conversation_session_id, execution_session_id,
            runtime_scope_key, output_contract_json, metadata_json,
            position_x, position_y, created_at, updated_at
        ) VALUES (
                'worker', 'mission-execution', 'worker', 'Worker', 'Do work',
            'running', 'profile-worker', 'version-worker', '', '',
            'worker-conversation-session', 'worker-runtime-session',
            'team:mission-legacy-runtime:node:worker', '{}', '{}',
            0, 0, 3, 4
        );

        INSERT INTO team_mission_run_bindings (
            run_id, mission_id, node_id, session_id, execution_session_id,
            runtime_scope_key, role, metadata_json, created_at, updated_at
        ) VALUES (
                'run-worker', 'mission-execution', 'worker',
            'worker-conversation-session', 'worker-runtime-session',
            'team:mission-legacy-runtime:node:worker', 'worker', '{}', 5, 6
        );
        """
    )
    conn.commit()
    conn.close()

    db = open_team_mission_state_store(db_path)
    try:
        node_row = db._conn.execute(  # noqa: SLF001 - schema migration assertion.
            """
            SELECT runtime_conversation_session_id, execution_session_id
            FROM team_mission_nodes
            WHERE mission_id = ? AND node_id = ?
            """,
            ("mission-execution", "worker"),
        ).fetchone()
        binding_row = db._conn.execute(  # noqa: SLF001 - schema migration assertion.
            """
            SELECT execution_session_id
            FROM team_mission_run_bindings
            WHERE run_id = ?
            """,
            ("run-worker",),
        ).fetchone()
        graph = db.get_team_mission_graph("mission-execution")
    finally:
        db.close()

    assert node_row is not None
    assert node_row["runtime_conversation_session_id"] == "worker-conversation-session"
    assert node_row["execution_session_id"] == "worker-runtime-session"
    assert binding_row is not None
    assert binding_row["execution_session_id"] == "worker-runtime-session"
    assert graph["nodes"][0]["runtime_conversation_session_id"] == "worker-conversation-session"
    assert graph["nodes"][0]["execution_session_id"] == "worker-runtime-session"
    assert graph["run_bindings"][0]["execution_session_id"] == "worker-runtime-session"


def test_team_mission_schema_rebuilds_canonical_conversation_session_id(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    fresh_timestamp = 9_999_999_999
    conn.executescript(
        f"""
        CREATE TABLE team_mission_conversations (
            conversation_id TEXT PRIMARY KEY,
            team_id TEXT,
            conversation_session_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            objective TEXT,
            workspace_id TEXT,
            workspace_path TEXT,
            status TEXT NOT NULL,
            active_mission_id TEXT,
            created_by_user_id TEXT,
            metadata_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        INSERT INTO team_mission_conversations (
            conversation_id, team_id, conversation_session_id, title, objective,
            workspace_id, workspace_path, status, active_mission_id,
            created_by_user_id, metadata_json, created_at, updated_at
        ) VALUES (
            'conversation-legacy', 'team-1', 'team-session-legacy',
            'Legacy Team', 'objective', 'workspace-1', '/tmp/workspace',
            'active', '', '', '{{}}', {fresh_timestamp}, {fresh_timestamp}
        );
        """
    )
    conn.commit()
    conn.close()

    db = open_team_mission_state_store(db_path)
    try:
        rows = db._conn.execute(  # noqa: SLF001 - schema migration assertion.
            'PRAGMA table_info("team_mission_conversations")'
        ).fetchall()
        columns = {str(row["name"]) for row in rows}
        column_flags = {str(row["name"]): dict(row) for row in rows}
        legacy_row = db._conn.execute(  # noqa: SLF001 - schema migration assertion.
            """
            SELECT conversation_id, conversation_session_id, title
            FROM team_mission_conversations
            WHERE conversation_id = ?
            """,
            ("conversation-legacy",),
        ).fetchone()
        created = db.ensure_team_mission_conversation(
            conversation_id="conversation-fresh",
            conversation_session_id="team-session-fresh",
            team_id="team-1",
            title="Fresh Team",
            objective="new objective",
            workspace_id="workspace-1",
            workspace_path="/tmp/workspace",
        )
    finally:
        db.close()

    assert "conversation_session_id" in columns
    assert "stable_session_id" not in columns
    assert int(column_flags["conversation_session_id"]["notnull"]) == 1
    assert legacy_row is not None
    assert legacy_row["conversation_session_id"] == "team-session-legacy"
    assert legacy_row["title"] == "Legacy Team"
    assert created["conversation_id"] == "conversation-fresh"
    assert created["conversation_session_id"] == "team-session-fresh"
