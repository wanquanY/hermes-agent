from __future__ import annotations

from pathlib import Path

from hermes_agent.domain.context_compaction import (
    ContextCompactionService,
    ContextScope,
    merge_actor_summaries,
)
from hermes_agent.storage.cli_session_store import open_cli_session_store


def _db(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-1", source="team_mission", transient=False)
    db.participants.ensure_participant(
        "conversation-1",
        participant_id="member:a",
        role="member",
    )
    return db


def _snapshot(db, *, activity_id: str, node_id: str = "", attempt_id: str = ""):
    return db.conversation_memory.create_snapshot(
        conversation_session_id="conversation-1",
        actor_participant_id="member:a",
        execution_scope_key="scope:a",
        activity_id=activity_id,
        activity_kind="mission" if activity_id.startswith("mission:") else "member_chat",
        node_id=node_id,
        attempt_id=attempt_id,
        conversation_revision=3,
    )


def test_actor_compaction_preserves_other_speaker_and_advances_only_actor(tmp_path: Path) -> None:
    db = _db(tmp_path)
    snapshot = _snapshot(db, activity_id="act-member_chat:conversation-1:a")
    scope = ContextScope(
        conversation_session_id="conversation-1",
        actor_participant_id="member:a",
        activity_id="act-member_chat:conversation-1:a",
        activity_kind="member_chat",
        snapshot_id=snapshot["snapshot_id"],
        conversation_revision=3,
    )
    result = ContextCompactionService(db.conversation_memory).checkpoint(
        scope,
        [
            {"role": "user", "content": "Please verify it", "message_id": "1", "participant_id": "user"},
            {"role": "assistant", "content": "B built it", "message_id": "2", "participant_id": "member:b"},
            {"role": "assistant", "content": "I will verify", "message_id": "3", "participant_id": "member:a"},
        ],
    )

    assert result["summary"]["actor_statements"][0]["participant_id"] == "member:a"
    other = result["summary"]["other_participant_statements"][0]
    assert other["participant_id"] == "member:b"
    assert other["statements"][0]["excerpt"] == "B built it"
    participant = db.participants.get_participant("conversation-1", "member:a")
    assert participant["transcript_cursor"] == 3
    assert participant["memory_revision"] == 1


def test_activity_and_node_compaction_use_separate_summary_scopes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    activity_snapshot = _snapshot(db, activity_id="mission:one")
    service = ContextCompactionService(db.conversation_memory)
    activity_scope = ContextScope(
        conversation_session_id="conversation-1",
        actor_participant_id="member:a",
        activity_id="mission:one",
        activity_kind="mission",
        snapshot_id=activity_snapshot["snapshot_id"],
        conversation_revision=5,
    )
    service.checkpoint(
        activity_scope,
        [{"role": "assistant", "content": "Planning complete", "message_id": "4", "participant_id": "member:a"}],
    )

    node_snapshot = _snapshot(
        db,
        activity_id="mission:one",
        node_id="worker-a",
        attempt_id="run-1",
    )
    node_scope = ContextScope(
        conversation_session_id="conversation-1",
        actor_participant_id="member:a",
        activity_id="mission:one",
        activity_kind="mission",
        node_id="worker-a",
        attempt_id="run-1",
        snapshot_id=node_snapshot["snapshot_id"],
        conversation_revision=6,
    )
    service.checkpoint(
        node_scope,
        [{"role": "assistant", "content": "Worker output", "message_id": "5", "participant_id": "member:a"}],
    )

    assert db.conversation_memory.latest_activity_summary("mission:one")["revision"] == 1
    assert db.conversation_memory.latest_node_attempt_summary(
        "mission:one", "worker-a", "run-1"
    )["revision"] == 1
    assert db.conversation_memory.latest_actor_summary("conversation-1", "member:a") == {}


def test_compaction_lease_is_scope_exclusive_and_explicitly_released(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = db.conversation_memory.try_acquire_compaction_lease("actor:conversation-1:member:a")
    second = db.conversation_memory.try_acquire_compaction_lease("actor:conversation-1:member:a")
    assert first
    assert second == ""
    assert db.conversation_memory.release_compaction_lease(
        "actor:conversation-1:member:a", first
    )
    assert db.conversation_memory.try_acquire_compaction_lease(
        "actor:conversation-1:member:a"
    )


def test_actor_summary_merge_deduplicates_by_source_event() -> None:
    record = {
        "participant_id": "member:a",
        "role": "assistant",
        "excerpt": "same",
        "source_event_ids": ["event-1"],
    }
    merged = merge_actor_summaries(
        {"actor_statements": [record], "source_event_ids": ["event-1"]},
        {"actor_statements": [record], "source_event_ids": ["event-1"]},
    )
    assert merged["actor_statements"] == [record]
    assert merged["source_event_ids"] == ["event-1"]


def test_actor_compaction_without_new_messages_is_a_noop(tmp_path: Path) -> None:
    db = _db(tmp_path)
    snapshot = _snapshot(db, activity_id="act-member_chat:conversation-1:a")
    scope = ContextScope(
        conversation_session_id="conversation-1",
        actor_participant_id="member:a",
        activity_id="act-member_chat:conversation-1:a",
        activity_kind="member_chat",
        snapshot_id=snapshot["snapshot_id"],
        conversation_revision=1,
    )
    service = ContextCompactionService(db.conversation_memory)
    first = service.checkpoint(
        scope,
        [{"role": "assistant", "content": "done", "message_id": "1", "participant_id": "member:a"}],
    )
    second = service.checkpoint(scope, [])
    participant = db.participants.get_participant("conversation-1", "member:a")

    assert second["summary_id"] == first["summary_id"]
    assert participant["transcript_cursor"] == 1
    assert participant["memory_revision"] == 1
