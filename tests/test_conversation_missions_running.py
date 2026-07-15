from pathlib import Path

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _create_conversation(db: CliSessionStore, conversation_id: str = "conv-1") -> dict:
    return db.upsert_team_mission_conversation(
        conversation_id=conversation_id,
        conversation_session_id=f"{conversation_id}-session",
        title="Conversation",
        status="active",
    )


def test_conv_with_one_active_mission_is_running(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)

    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-1")

    assert db.has_active_mission("conv-1") is True
    assert db.active_mission_ids("conv-1") == ["mission-1"]


def test_conv_with_two_active_missions_is_running(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)

    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-1")
    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-2")

    assert db.has_active_mission("conv-1") is True
    assert set(db.active_mission_ids("conv-1")) == {"mission-1", "mission-2"}


def test_conv_with_all_completed_missions_is_not_running(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)

    for index in range(3):
        mission_id = f"mission-{index}"
        db.add_mission_to_conversation(conversation_id="conv-1", mission_id=mission_id)
        db.set_conversation_mission_status(
            conversation_id="conv-1",
            mission_id=mission_id,
            status="completed",
        )

    assert db.has_active_mission("conv-1") is False
    assert db.active_mission_ids("conv-1") == []


def test_conv_with_mix_of_active_and_completed_is_running(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)

    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-active")
    db.add_mission_to_conversation(conversation_id="conv-1", mission_id="mission-completed")
    db.set_conversation_mission_status(
        conversation_id="conv-1",
        mission_id="mission-completed",
        status="completed",
    )

    assert db.has_active_mission("conv-1") is True
    assert db.active_mission_ids("conv-1") == ["mission-active"]


def test_conv_with_no_missions_is_not_running(tmp_path: Path):
    db = _db(tmp_path)
    _create_conversation(db)

    assert db.has_active_mission("conv-1") is False
    assert db.active_mission_ids("conv-1") == []


def test_sidebar_session_index_running_uses_conversation_missions(tmp_path: Path):
    db = _db(tmp_path)
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        conversation_session_id="conv-1-session",
        title="Conversation",
        status="active",
        active_mission_id="mission-active",
    )
    db._conn.execute(  # noqa: SLF001 - force a stale legacy/index projection.
        "UPDATE team_mission_conversations SET active_mission_id = '' WHERE conversation_id = ?",
        ("conv-1",),
    )
    db._conn.execute(  # noqa: SLF001 - list_session_index must repair at read time.
        "UPDATE session_index SET running = 0, mission_id = '' WHERE conversation_id = ?",
        ("conv-1",),
    )
    db._conn.commit()  # noqa: SLF001

    rows = db.session_index.list(limit=10)["sessions"]
    item = next(row for row in rows if row["conversation_id"] == "conv-1")

    assert item["running"] is True
