from pathlib import Path

from hermes_state import SessionDB
from hermes_team_mission.state.schema import migrate_active_mission_id_to_conversation_missions


def _create_conversation(db: SessionDB, conversation_id: str = "conv-1") -> dict:
    return db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=f"{conversation_id}-session",
        title="Conversation",
        status="active",
    )


def test_create_conversation_then_add_mission(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    conversation = _create_conversation(db)

    row = db.add_mission_to_conversation(
        conversation_id=conversation["conversation_id"],
        mission_id="mission-1",
    )

    assert row["conversation_id"] == "conv-1"
    assert row["mission_id"] == "mission-1"
    assert row["status"] == "active"
    assert [item["mission_id"] for item in db.list_conversation_missions("conv-1")] == ["mission-1"]


def test_add_multiple_missions_to_same_conversation_all_active(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _create_conversation(db)

    for mission_id in ("mission-1", "mission-2", "mission-3"):
        db.add_mission_to_conversation(conversation_id="conv-1", mission_id=mission_id)

    rows = db.list_conversation_missions("conv-1", status="active")
    assert {row["mission_id"] for row in rows} == {"mission-1", "mission-2", "mission-3"}
    assert {row["status"] for row in rows} == {"active"}


def test_set_mission_status_completed_leaves_others_active(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _create_conversation(db)
    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-1")
    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-2")

    updated = db.set_conversation_mission_status(
        conversation_id="conv-1",
        mission_id="mission-1",
        status="completed",
    )

    assert updated is not None
    assert updated["status"] == "completed"
    active_rows = db.list_conversation_missions("conv-1", status="active")
    assert [row["mission_id"] for row in active_rows] == ["mission-2"]
    conversation = db.get_team_mission_conversation("conv-1")
    assert conversation["status"] == "active"


def test_remove_mission_keeps_others(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _create_conversation(db)
    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-1")
    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-2")

    assert db.remove_mission_from_conversation(conversation_id="conv-1", mission_id="mission-1") is True
    assert db.remove_mission_from_conversation(conversation_id="conv-1", mission_id="mission-missing") is False
    assert [row["mission_id"] for row in db.list_conversation_missions("conv-1")] == ["mission-2"]


def test_idempotent_add_does_not_duplicate(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    _create_conversation(db)

    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-1")
    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-1")

    rows = db.list_conversation_missions("conv-1")
    assert [row["mission_id"] for row in rows] == ["mission-1"]


def test_migration_backfills_existing_active_mission_id(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db._conn.execute(  # noqa: SLF001 - direct legacy-row setup for migration coverage.
        """
        INSERT INTO team_mission_conversations (
            conversation_id, conversation_session_id, title, status, active_mission_id,
            metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("conv-legacy", "conv-legacy-session", "Legacy", "active", "mission-legacy", "", 123.0, 456.0),
    )
    db.close()

    migrated = SessionDB(db_path)

    rows = migrated.list_conversation_missions("conv-legacy")
    assert len(rows) == 1
    assert rows[0]["mission_id"] == "mission-legacy"
    assert rows[0]["status"] == "active"
    assert rows[0]["added_at"] == 123.0


def test_migration_is_idempotent(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db._conn.execute(  # noqa: SLF001 - direct legacy-row setup for migration coverage.
        """
        INSERT INTO team_mission_conversations (
            conversation_id, conversation_session_id, title, status, active_mission_id,
            metadata_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("conv-legacy", "conv-legacy-session", "Legacy", "active", "mission-legacy", "", 123.0, 456.0),
    )

    migrate_active_mission_id_to_conversation_missions(db._conn.cursor())  # noqa: SLF001
    migrate_active_mission_id_to_conversation_missions(db._conn.cursor())  # noqa: SLF001

    rows = db.list_conversation_missions("conv-legacy")
    assert [row["mission_id"] for row in rows] == ["mission-legacy"]


def test_upsert_conversation_projects_active_mission_from_join_table(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")

    conversation = db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        conversation_session_id="conv-1-session",
        title="Conversation",
        status="active",
        active_mission_id="mission-x",
    )

    assert conversation["active_mission_id"] == "mission-x"
    rows = db.list_conversation_missions("conv-1")
    assert [row["mission_id"] for row in rows] == ["mission-x"]
    assert rows[0]["status"] == "active"


def test_legacy_active_mission_id_field_is_not_written_by_new_upsert(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        conversation_session_id="conv-1-session",
        title="Conversation",
        status="active",
        active_mission_id="mission-x",
    )

    conversation = db.get_team_mission_conversation("conv-1")
    row = db._conn.execute(  # noqa: SLF001 - storage compatibility contract.
        """
        SELECT active_mission_id
        FROM team_mission_conversations
        WHERE conversation_id = ?
        """,
        ("conv-1",),
    ).fetchone()

    assert conversation["active_mission_id"] == "mission-x"
    assert (row["active_mission_id"] or "") == ""
