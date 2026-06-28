from __future__ import annotations

from typing import Any

from hermes_state import SessionDB
from tui_gateway.services.transcript_projector import (
    InMemoryTranscriptProjectionStore,
    ProjectionKey,
    SessionDBTranscriptProjectionStore,
    TranscriptProjector,
    conversation_message_id_for,
    conversation_user_message_id_for,
)


SESSION_ID = "team-session-team-conversation-1"
PARTICIPANT_ID = "member:alice"


def _projector() -> tuple[TranscriptProjector, InMemoryTranscriptProjectionStore]:
    store = InMemoryTranscriptProjectionStore()
    return TranscriptProjector(store), store


def _event(
    event_type: str,
    *,
    run_id: str = "run-1",
    turn_id: str = "turn-1",
    message_seq_in_run: int | str | None = 1,
    participant_id: str = PARTICIPANT_ID,
    seq: int = 1,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": event_type,
        "stored_session_id": SESSION_ID,
        "session_id": "runtime-session-1",
        "run_id": run_id,
        "turn_id": turn_id,
        "participant_id": participant_id,
        "seq": seq,
        "activity_kind": "member_chat",
        "payload": dict(payload or {}),
    }
    if message_seq_in_run is not None:
        body["message_seq_in_run"] = message_seq_in_run
    if not participant_id:
        body.pop("participant_id", None)
    return body


def test_message_start_opens_projected_assistant_message() -> None:
    projector, store = _projector()

    result = projector.reduce(_event("message.start", message_seq_in_run=1))

    expected_key = ProjectionKey(SESSION_ID, "run-1", "1")
    assert result.applied is True
    assert result.action == "created"
    assert result.message is not None
    assert result.message.conversation_message_id == conversation_message_id_for(expected_key)
    assert result.message.content == ""
    assert result.message.status == "streaming"
    assert result.message.metadata["message_seq_in_run"] == "1"
    assert store.get_projected_message(expected_key) is not None


def test_delta_and_complete_update_same_message_seq() -> None:
    projector, store = _projector()

    projector.reduce(_event("message.start", message_seq_in_run=1))
    delta = projector.reduce(
        _event(
            "message.delta",
            message_seq_in_run=1,
            seq=2,
            payload={"delta": "draft", "offset": 0},
        )
    )
    complete = projector.reduce(
        _event(
            "message.complete",
            message_seq_in_run=1,
            seq=3,
            payload={"text": "canonical final", "status": "complete"},
        )
    )

    assert delta.applied is True
    assert complete.applied is True
    assert complete.message is not None
    assert complete.message.content == "canonical final"
    assert complete.message.status == "completed"
    assert len(store.list_messages()) == 1
    stored = store.get_projected_message(ProjectionKey(SESSION_ID, "run-1", "1"))
    assert stored is not None
    assert stored.content == "canonical final"
    assert stored.metadata["source_event_seq"] == "3"


def test_reasoning_delta_updates_same_projected_assistant_message() -> None:
    projector, store = _projector()

    projector.reduce(_event("message.start", message_seq_in_run=1))
    projector.reduce(
        _event(
            "reasoning.delta",
            message_seq_in_run=1,
            seq=2,
            payload={"delta": "先分析", "text": "先分析", "offset": 0},
        )
    )
    projector.reduce(
        _event(
            "message.delta",
            message_seq_in_run=1,
            seq=3,
            payload={"delta": "答案", "offset": 0},
        )
    )
    complete = projector.reduce(
        _event(
            "message.complete",
            message_seq_in_run=1,
            seq=4,
            payload={"text": "最终答案", "status": "complete"},
        )
    )

    assert complete.applied is True
    assert complete.message is not None
    assert complete.message.content == "最终答案"
    assert complete.message.reasoning == "先分析"
    assert len(store.list_messages()) == 1
    stored = store.get_projected_message(ProjectionKey(SESSION_ID, "run-1", "1"))
    assert stored is not None
    assert stored.content == "最终答案"
    assert stored.reasoning == "先分析"


def test_reasoning_delta_before_message_start_is_preserved() -> None:
    projector, store = _projector()

    projector.reduce(
        _event(
            "reasoning.delta",
            message_seq_in_run=1,
            seq=1,
            payload={"delta": "提前思考", "offset": 0},
        )
    )
    projector.reduce(_event("message.start", message_seq_in_run=1, seq=2))
    projector.reduce(
        _event(
            "message.complete",
            message_seq_in_run=1,
            seq=3,
            payload={"text": "答案", "status": "complete"},
        )
    )

    assert len(store.list_messages()) == 1
    stored = store.get_projected_message(ProjectionKey(SESSION_ID, "run-1", "1"))
    assert stored is not None
    assert stored.content == "答案"
    assert stored.reasoning == "提前思考"
    assert stored.status == "completed"


def test_reasoning_snapshot_events_are_idempotent() -> None:
    projector, store = _projector()

    projector.reduce(_event("message.start", message_seq_in_run=1))
    projector.reduce(
        _event(
            "reasoning.delta",
            message_seq_in_run=1,
            seq=2,
            payload={"text": "The user asks."},
        )
    )
    projector.reduce(
        _event(
            "reasoning.delta",
            message_seq_in_run=1,
            seq=3,
            payload={"text": "The user asks. I should answer as frontend."},
        )
    )
    projector.reduce(
        _event(
            "reasoning.delta",
            message_seq_in_run=1,
            seq=4,
            payload={"text": "The user asks. I should answer as frontend."},
        )
    )

    stored = store.get_projected_message(ProjectionKey(SESSION_ID, "run-1", "1"))
    assert stored is not None
    assert stored.reasoning == "The user asks. I should answer as frontend."
    assert len(store.list_messages()) == 1


def test_reasoning_offset_replay_is_idempotent() -> None:
    projector, store = _projector()
    event = _event(
        "reasoning.delta",
        message_seq_in_run=1,
        seq=2,
        payload={"delta": "重复片段", "offset": 0},
    )

    projector.reduce(_event("message.start", message_seq_in_run=1))
    first = projector.reduce(event)
    second = projector.reduce(event)

    assert first.applied is True
    assert second.applied is True
    stored = store.get_projected_message(ProjectionKey(SESSION_ID, "run-1", "1"))
    assert stored is not None
    assert stored.reasoning == "重复片段"


def test_same_run_different_message_seq_creates_multiple_assistant_messages() -> None:
    projector, store = _projector()

    projector.reduce(
        _event(
            "message.complete",
            message_seq_in_run=1,
            seq=10,
            payload={"text": "first visible assistant segment", "status": "complete"},
        )
    )
    projector.reduce(
        _event(
            "message.complete",
            message_seq_in_run=2,
            seq=20,
            payload={"text": "second visible assistant segment", "status": "complete"},
        )
    )

    messages = sorted(store.list_messages(), key=lambda item: item.metadata["message_seq_in_run"])
    assert [message.content for message in messages] == [
        "first visible assistant segment",
        "second visible assistant segment",
    ]
    assert [message.metadata["message_seq_in_run"] for message in messages] == ["1", "2"]
    assert messages[0].metadata["run_id"] == messages[1].metadata["run_id"] == "run-1"
    assert messages[0].conversation_message_id != messages[1].conversation_message_id


def test_duplicate_complete_for_same_message_seq_is_idempotent() -> None:
    projector, store = _projector()
    complete = _event(
        "message.complete",
        message_seq_in_run=1,
        seq=3,
        payload={"text": "final once", "status": "complete"},
    )

    first = projector.reduce(complete)
    second = projector.reduce(complete)

    assert first.action == "created"
    assert second.action == "updated"
    assert len(store.list_messages()) == 1
    assert store.list_messages()[0].content == "final once"


def test_same_participant_same_message_seq_in_different_runs_is_not_merged() -> None:
    projector, store = _projector()

    projector.reduce(
        _event(
            "message.complete",
            run_id="run-a",
            turn_id="turn-a",
            message_seq_in_run=1,
            seq=1,
            payload={"text": "run a final", "status": "complete"},
        )
    )
    projector.reduce(
        _event(
            "message.complete",
            run_id="run-b",
            turn_id="turn-b",
            message_seq_in_run=1,
            seq=2,
            payload={"text": "run b final", "status": "complete"},
        )
    )

    messages = sorted(store.list_messages(), key=lambda item: item.metadata["run_id"])
    assert [message.content for message in messages] == ["run a final", "run b final"]
    assert [message.metadata["run_id"] for message in messages] == ["run-a", "run-b"]
    assert all(message.metadata["message_seq_in_run"] == "1" for message in messages)


def test_missing_participant_id_does_not_project_or_fallback_to_leader() -> None:
    projector, store = _projector()

    result = projector.reduce(
        _event(
            "message.complete",
            participant_id="",
            payload={"text": "must not be visible as leader", "status": "complete"},
        )
    )

    assert result.applied is False
    assert result.action == "skipped"
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["participant-unresolved"]
    assert store.list_messages() == []


def test_legacy_complete_without_message_seq_uses_source_event_seq_identity() -> None:
    projector, store = _projector()

    result = projector.reduce(
        _event(
            "message.complete",
            message_seq_in_run=None,
            seq=77,
            payload={"text": "legacy final", "status": "complete"},
        )
    )

    assert result.applied is True
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["legacy-message-identity"]
    assert result.message is not None
    assert result.message.metadata["legacy_message_identity"] is True
    assert result.message.metadata["message_seq_in_run"] == "legacy-source-seq:77"
    assert len(store.list_messages()) == 1


def test_session_db_store_updates_same_projected_message(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id=SESSION_ID, source="dovie")
        projector = TranscriptProjector(SessionDBTranscriptProjectionStore(db))

        projector.reduce(_event("message.start", message_seq_in_run=1, seq=1))
        projector.reduce(
            _event(
                "message.delta",
                message_seq_in_run=1,
                seq=2,
                payload={"delta": "draft", "offset": 0},
            )
        )
        projector.reduce(
            _event(
                "reasoning.delta",
                message_seq_in_run=1,
                seq=3,
                payload={"delta": "reasoning draft", "offset": 0},
            )
        )
        projector.reduce(
            _event(
                "message.complete",
                message_seq_in_run=1,
                seq=4,
                payload={"text": "canonical final", "status": "complete"},
            )
        )

        messages = db.get_messages_as_conversation(SESSION_ID, include_storage_metadata=True)
        assert len(messages) == 1
        assert messages[0]["content"] == "canonical final"
        assert messages[0]["reasoning"] == "reasoning draft"
        assert messages[0]["participant_id"] == PARTICIPANT_ID
        assert messages[0]["conversation_message_id"] == conversation_message_id_for(
            ProjectionKey(SESSION_ID, "run-1", "1")
        )
        assert messages[0]["metadata"]["projection_status"] == "completed"
        read_model = db.get_conversation_message_read_model(SESSION_ID, include_storage_metadata=True)
        assert len(read_model) == 1
        assert read_model[0]["reasoning"] == "reasoning draft"
        assert db.get_session(SESSION_ID)["message_count"] == 1
    finally:
        db.close()


def test_session_db_store_keeps_multiple_messages_in_one_run(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id=SESSION_ID, source="dovie")
        projector = TranscriptProjector(SessionDBTranscriptProjectionStore(db))

        projector.reduce(
            _event(
                "message.complete",
                message_seq_in_run=1,
                seq=10,
                payload={"text": "first segment", "status": "complete"},
            )
        )
        projector.reduce(
            _event(
                "message.complete",
                message_seq_in_run=2,
                seq=20,
                payload={"text": "second segment", "status": "complete"},
            )
        )

        messages = db.get_messages_as_conversation(SESSION_ID, include_storage_metadata=True)
        assert [message["content"] for message in messages] == ["first segment", "second segment"]
        assert [message["metadata"]["message_seq_in_run"] for message in messages] == ["1", "2"]
        assert len({message["conversation_message_id"] for message in messages}) == 2
        assert db.get_session(SESSION_ID)["message_count"] == 2
    finally:
        db.close()


def test_record_event_projects_team_conversation_messages(tmp_path) -> None:
    from tui_gateway.services.run_control import record_event

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id=SESSION_ID, source="team_mission")
        db.upsert_session_index(
            session_id=SESSION_ID,
            source="team_mission",
            conversation_kind="team",
            started_at=1.0,
            updated_at=1.0,
        )

        record_event(
            _event("message.start", message_seq_in_run=1, seq=1),
            db=db,
        )
        record_event(
            _event(
                "message.delta",
                message_seq_in_run=1,
                seq=2,
                payload={"delta": "streaming", "offset": 0},
            ),
            db=db,
        )
        record_event(
            _event(
                "reasoning.delta",
                message_seq_in_run=1,
                seq=3,
                payload={"delta": "projected reasoning", "offset": 0},
            ),
            db=db,
        )
        record_event(
            _event(
                "message.complete",
                message_seq_in_run=1,
                seq=4,
                payload={"text": "canonical final", "status": "complete"},
            ),
            db=db,
        )

        messages = db.get_messages_as_conversation(SESSION_ID, include_storage_metadata=True)
        assert len(messages) == 1
        assert messages[0]["content"] == "canonical final"
        assert messages[0]["reasoning"] == "projected reasoning"
        assert messages[0]["participant_id"] == PARTICIPANT_ID
        assert messages[0]["conversation_message_id"] == conversation_message_id_for(
            ProjectionKey(SESSION_ID, "run-1", "1")
        )
        assert messages[0]["metadata"]["projection_status"] == "completed"
        assert db.get_session(SESSION_ID)["message_count"] == 1
    finally:
        db.close()


def test_record_event_does_not_project_direct_conversation_messages(tmp_path) -> None:
    from tui_gateway.services.run_control import record_event

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id=SESSION_ID, source="tui")
        db.upsert_session_index(
            session_id=SESSION_ID,
            source="tui",
            conversation_kind="direct",
            started_at=1.0,
            updated_at=1.0,
        )

        record_event(
            _event(
                "message.complete",
                message_seq_in_run=1,
                seq=1,
                payload={"text": "direct final", "status": "complete"},
            ),
            db=db,
        )

        assert db.get_messages_as_conversation(SESSION_ID, include_storage_metadata=True) == []
        assert db.get_session(SESSION_ID)["message_count"] == 0
    finally:
        db.close()


def test_team_read_model_collapses_legacy_worker_flush_shadows(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id=SESSION_ID, source="team_mission")
        db.upsert_session_index(
            session_id=SESSION_ID,
            source="team_mission",
            conversation_kind="team",
            started_at=1.0,
            updated_at=1.0,
        )
        run_id = "team-member-run-shadow"
        turn_id = "team-member-turn-shadow"
        prompt = "你是谁？我在测试团队会话不同成员的发言功能，你如实告诉我你的角色"
        canonical_reply = "我是后端工程师，负责 API 设计。"
        legacy_reply = "我是后端工程师，负责 API设计。"

        db.append_message(SESSION_ID, role="user", content=prompt)
        projector = TranscriptProjector(SessionDBTranscriptProjectionStore(db))
        projector.reduce(
            _event(
                "message.complete",
                run_id=run_id,
                turn_id=turn_id,
                message_seq_in_run=1,
                seq=10,
                payload={"text": canonical_reply, "status": "complete"},
            )
        )
        db.append_message(
            SESSION_ID,
            role="user",
            content=prompt,
            participant_id=PARTICIPANT_ID,
            metadata={
                "run_id": run_id,
                "turn_id": turn_id,
                "participant_id": PARTICIPANT_ID,
            },
        )
        db.append_message(
            SESSION_ID,
            role="assistant",
            content=legacy_reply,
            participant_id=PARTICIPANT_ID,
            metadata={
                "run_id": run_id,
                "turn_id": turn_id,
                "participant_id": PARTICIPANT_ID,
            },
        )

        raw_messages = db.get_messages_as_conversation(SESSION_ID, include_storage_metadata=True)
        read_model = db.get_conversation_message_read_model(SESSION_ID, include_storage_metadata=True)
        page = db.get_messages_page_as_conversation(SESSION_ID, limit=10)

        assert len(raw_messages) == 4
        assert [message["role"] for message in read_model] == ["user", "assistant"]
        assert [message["content"] for message in read_model] == [prompt, canonical_reply]
        assert read_model[1]["conversation_message_id"] == conversation_message_id_for(
            ProjectionKey(SESSION_ID, run_id, "1")
        )
        assert [message["role"] for message in page["messages"]] == ["user", "assistant"]
        assert [message["content"] for message in page["messages"]] == [prompt, canonical_reply]
        assert page["pageInfo"]["totalCount"] == 2
    finally:
        db.close()


def test_team_read_model_prefers_projected_user_submission_over_worker_shadow(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id=SESSION_ID, source="team_mission")
        db.upsert_session_index(
            session_id=SESSION_ID,
            source="team_mission",
            conversation_kind="team",
            started_at=1.0,
            updated_at=1.0,
        )
        run_id = "team-leader-run-user"
        turn_id = "team-leader-turn-user"
        prompt = "第一条团队会话用户消息"
        conversation_message_id = conversation_user_message_id_for(
            session_id=SESSION_ID,
            run_id=run_id,
            turn_id=turn_id,
        )

        db.upsert_projected_conversation_message(
            session_id=SESSION_ID,
            conversation_message_id=conversation_message_id,
            role="user",
            content=prompt,
            participant_id="",
            metadata={
                "source": "team_mission.message.submit",
                "message_kind": "user_submission",
                "run_id": run_id,
                "turn_id": turn_id,
            },
            status="completed",
        )
        db.append_message(
            SESSION_ID,
            role="user",
            content=prompt,
            metadata={"run_id": run_id, "turn_id": turn_id},
        )

        raw_messages = db.get_messages_as_conversation(SESSION_ID, include_storage_metadata=True)
        read_model = db.get_conversation_message_read_model(SESSION_ID, include_storage_metadata=True)

        assert len(raw_messages) == 2
        assert [message["role"] for message in read_model] == ["user"]
        assert read_model[0]["content"] == prompt
        assert read_model[0]["conversation_message_id"] == conversation_message_id
    finally:
        db.close()


def test_direct_read_model_preserves_repeated_user_messages(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="direct-session-1", source="tui")
        db.upsert_session_index(
            session_id="direct-session-1",
            source="tui",
            conversation_kind="direct",
            started_at=1.0,
            updated_at=1.0,
        )
        db.append_message("direct-session-1", role="user", content="repeat")
        db.append_message("direct-session-1", role="user", content="repeat")

        read_model = db.get_conversation_message_read_model("direct-session-1")

        assert [message["content"] for message in read_model] == ["repeat", "repeat"]
    finally:
        db.close()
