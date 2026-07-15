import json
from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_schema_migration_compacts_legacy_conversation_status_event_json(tmp_path: Path):
    from hermes_agent.composition.migrations import CURRENT_SCHEMA_VERSION

    db_path = tmp_path / "state.db"
    db = open_cli_session_store(db_path)
    try:
        db.upsert_team_mission_conversation(
            conversation_id="conversation-legacy",
            conversation_session_id="team-session-legacy",
            team_id="team-1",
            active_mission_id="mission-legacy",
            title="Legacy Mission",
        )
        db.upsert_team_mission(
            mission_id="mission-legacy",
            conversation_id="conversation-legacy",
            team_id="team-1",
            title="Legacy Mission",
            mode="supervised_mission",
            status="completed",
            leader_session_id="team-session-legacy",
            created_at=100,
            updated_at=200,
            completed_at=300,
        )
        stored = db.append_team_mission_conversation_status_event(
            mission_id="mission-legacy",
            source_event={
                "type": "mission.completed",
                "seq": 7,
                "timestamp": 300,
            },
            source_mission_seq=7,
        )
        projection = db.get_team_mission_conversation_status_projection("conversation-legacy")
        projection["task_frames"] = [{"id": "frame-1", "markdown": "x" * 4000}]
        projection["run_session_ids"] = [f"team:mission-legacy:node:{idx}" for idx in range(100)]
        projection["final_deliverables"] = [{"summary": "y" * 4000}]
        legacy_event = dict(stored)
        legacy_payload = dict(stored.get("payload") or {})
        legacy_payload["schemaVersion"] = 1
        legacy_payload["projection"] = projection
        legacy_payload["conversation"] = projection
        legacy_event["payload"] = legacy_payload
        legacy_event_json = json.dumps(legacy_event, ensure_ascii=False)
        db._conn.execute(  # noqa: SLF001 - simulate pre-39 fat status event rows.
            """
            UPDATE team_mission_events
               SET event_json = ?
             WHERE mission_id = ?
               AND seq = ?
            """,
            (legacy_event_json, "mission-legacy", stored["seq"]),
        )
        db._conn.execute("UPDATE schema_version SET version = ?", (38,))  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001 - force the declarative migration to replay.
            "DELETE FROM applied_migrations WHERE version = 39"
        )
    finally:
        db.close()

    migrated = open_cli_session_store(db_path)
    try:
        row = migrated._conn.execute(  # noqa: SLF001 - migration contract assertion.
            """
            SELECT event_json
              FROM team_mission_events
             WHERE mission_id = ?
             ORDER BY seq DESC
             LIMIT 1
            """,
            ("mission-legacy",),
        ).fetchone()
        event = json.loads(row["event_json"])
        payload = event["payload"]
        projection = payload["projection"]
        assert payload["schemaVersion"] == 2
        assert "conversation" not in payload
        assert "task_frames" not in projection
        assert "run_session_ids" not in projection
        assert "final_deliverables" not in projection
        assert len(row["event_json"]) < len(legacy_event_json) // 3
        version = migrated._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0]  # noqa: SLF001
        assert version == CURRENT_SCHEMA_VERSION
    finally:
        migrated.close()


def test_startup_maintenance_repairs_terminal_team_session_stale_running(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = open_cli_session_store(db_path)
    try:
        db.upsert_team_mission(
            mission_id="mission-done",
            conversation_id="conversation-done",
            team_id="team-1",
            title="Done Mission",
            mode="supervised_mission",
            status="completed",
            leader_session_id="team-session-done",
            completed_at=100,
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-done",
            conversation_session_id="team-session-done",
            team_id="team-1",
            active_mission_id="mission-done",
            title="Done Mission",
        )
        db.runs.upsert(
            run_id="leader-run-done",
            session_id="team-session-done",
            status="completed",
        )
        db.session_index.upsert(
            session_id="team-session-done",
            session_kind="team_mission",
            conversation_kind="team",
            conversation_id="conversation-done",
            mission_id="mission-done",
            running=True,
            status="running",
            active_run_id="leader-run-done",
            active_execution_session_id="runtime-done",
            started_at=1.0,
            updated_at=2.0,
        )
        stuck = db._conn.execute(  # noqa: SLF001 - verify simulated production residue.
            "SELECT running, status, active_run_id FROM session_index WHERE session_id = ?",
            ("team-session-done",),
        ).fetchone()
        assert int(stuck["running"]) == 1
        assert stuck["status"] == "running"
        assert stuck["active_run_id"] == "leader-run-done"
    finally:
        db.close()

    reopened = open_cli_session_store(db_path)
    try:
        healed = reopened._conn.execute(  # noqa: SLF001 - startup maintenance contract.
            "SELECT running, status, active_run_id, active_execution_session_id FROM session_index WHERE session_id = ?",
            ("team-session-done",),
        ).fetchone()
        assert int(healed["running"]) == 0
        assert healed["status"] == "idle"
        assert healed["active_run_id"] == ""
        assert healed["active_execution_session_id"] == ""
    finally:
        reopened.close()


def test_startup_maintenance_preserves_valid_empty_team_conversation(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = open_cli_session_store(db_path)
    try:
        db.upsert_team_mission_conversation(
            conversation_id="conversation-empty",
            conversation_session_id="team-session-empty",
            team_id="team-1",
            title="Prepared Conversation",
        )
        db._conn.execute(  # noqa: SLF001 - simulate an old but valid prepared row.
            """
            UPDATE team_mission_conversations
               SET created_at = 1, updated_at = 1
             WHERE conversation_id = ?
            """,
            ("conversation-empty",),
        )
        db._conn.commit()  # noqa: SLF001
    finally:
        db.close()

    reopened = open_cli_session_store(db_path)
    try:
        conversation = reopened.get_team_mission_conversation("conversation-empty")
        assert conversation is not None
        assert conversation["conversation_session_id"] == "team-session-empty"
    finally:
        reopened.close()
