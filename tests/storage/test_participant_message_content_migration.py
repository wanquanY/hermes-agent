from __future__ import annotations

import json
from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_migration_removes_legacy_identity_from_canonical_message_content(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    db = open_cli_session_store(db_path)
    db.sessions.create("team-session-1", source="team_mission")
    db.participants.upsert_conversation_participant(
        conversation_session_id="team-session-1",
        participant_id="leader:conversation-1",
        role="leader",
        display_name="小多",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id="team-session-1",
        participant_id="member:alice",
        role="member",
        display_name="Alice",
    )
    db.messages.append(
        "team-session-1",
        role="assistant",
        content=(
            "[assistant | You | leader:conversation-1]\n"
            "[assistant | You | leader:conversation-1]\n"
            "任务已经取消。"
        ),
        participant_id="leader:conversation-1",
    )
    db.messages.append(
        "team-session-1",
        role="user",
        content="[assistant | Alice | member:alice]\n成员回复",
        participant_id="member:alice",
        metadata={
            "speaker_envelope_version": "participant-speaker-v1",
            "speaker_participant_id": "member:alice",
            "speaker_original_role": "assistant",
            "speaker_projected_role": "user",
        },
    )
    db.messages.append(
        "team-session-1",
        role="user",
        content="[assistant | quoted | unrelated:id]\n这是用户引用，不应修改",
        participant_id="user",
    )
    db._conn.execute("DELETE FROM applied_migrations WHERE version = 51")
    db._conn.execute("UPDATE schema_version SET version = 50")
    db._conn.commit()
    db.close()

    migrated = open_cli_session_store(db_path)
    rows = migrated._conn.execute(  # noqa: SLF001
        "SELECT role, content, metadata_json FROM messages "
        "WHERE session_id = ? ORDER BY id",
        ("team-session-1",),
    ).fetchall()
    migrated.close()

    assert [(row["role"], row["content"]) for row in rows] == [
        ("assistant", "任务已经取消。"),
        ("assistant", "成员回复"),
        ("user", "[assistant | quoted | unrelated:id]\n这是用户引用，不应修改"),
    ]
    first_metadata = json.loads(rows[0]["metadata_json"])
    second_metadata = json.loads(rows[1]["metadata_json"])
    assert first_metadata["participant_content_migration"] == {
        "removed_legacy_envelopes": 2,
        "version": 51,
    }
    assert second_metadata["speaker_participant_id"] == "member:alice"
    assert "speaker_envelope_version" not in second_metadata
    assert "speaker_projected_role" not in second_metadata
