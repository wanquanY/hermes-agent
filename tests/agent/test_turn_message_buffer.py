"""Tests for the canonical history/current-input boundary."""

from agent.turn_message_buffer import TurnMessageBuffer, reanchor_current_input


def test_bind_persisted_current_input_by_top_level_identity():
    history = [
        {"role": "user", "content": "first", "conversation_message_id": "msg-1"},
        {
            "role": "user",
            "content": "current",
            "conversation_message_id": "msg-current",
        },
    ]
    messages = TurnMessageBuffer.from_history(history)

    current = messages.bind_persisted_current_input("msg-current")

    assert current is history[1]
    assert messages.current_input_message is history[1]
    assert messages.current_input_index == 1
    assert messages.persist_from_index == 2


def test_bind_persisted_current_input_by_projected_metadata_identity():
    current = {
        "role": "user",
        "content": "current",
        "metadata": {"conversation_message_id": "msg-current"},
    }
    messages = TurnMessageBuffer.from_history([current])

    bound = messages.bind_persisted_current_input("msg-current")

    assert bound is current
    assert messages.current_input_index == 0


def test_bind_persisted_current_input_rejects_assistant_with_same_identity():
    messages = TurnMessageBuffer.from_history(
        [
            {
                "role": "assistant",
                "content": "not user input",
                "conversation_message_id": "msg-current",
            }
        ]
    )

    assert messages.bind_persisted_current_input("msg-current") is None
    assert messages.current_input_index is None


def test_append_current_input_for_runtime_owned_turn():
    messages = TurnMessageBuffer.from_history(
        [{"role": "user", "content": "history"}]
    )

    current = messages.append_current_input(
        "new input",
        metadata={"turn_id": "turn-2"},
    )

    assert current == {
        "role": "user",
        "content": "new input",
        "metadata": {"turn_id": "turn-2"},
    }
    assert messages.current_input_index == 1
    assert messages.persist_from_index == 1


def test_append_existing_current_input_preserves_cli_handoff_identity():
    staged = {
        "role": "user",
        "content": "persisted text",
        "metadata": {"source": "cli"},
    }
    messages = TurnMessageBuffer.from_history(
        [{"role": "assistant", "content": "history"}]
    )

    current = messages.append_existing_current_input(
        staged,
        api_content="API-only prefix: persisted text",
        metadata={"turn_id": "turn-2"},
    )

    assert current is staged
    assert messages[-1] is staged
    assert messages.current_input_message is staged
    assert messages.current_input_index == 1
    assert staged == {
        "role": "user",
        "content": "API-only prefix: persisted text",
        "metadata": {"source": "cli", "turn_id": "turn-2"},
    }


def test_bound_persisted_input_stays_before_new_message_persistence_boundary():
    history = [
        {
            "role": "user",
            "content": "current",
            "conversation_message_id": "msg-current",
        }
    ]
    messages = TurnMessageBuffer.from_history(history)
    messages.bind_persisted_current_input("msg-current")
    messages.append({"role": "assistant", "content": "reply"})

    assert messages.persist_from_index == 1
    assert list(messages[messages.persist_from_index :]) == [
        {"role": "assistant", "content": "reply"}
    ]


def test_reanchor_current_input_prefers_conversation_identity_after_rewrite():
    current = {
        "role": "user",
        "content": "rewritten by compression",
        "metadata": {"conversation_message_id": "current-id"},
    }
    messages = [
        {"role": "user", "content": "same clean content"},
        current,
    ]

    index, anchored = reanchor_current_input(
        messages,
        conversation_message_id="current-id",
        content_candidates=("same clean content",),
    )

    assert index == 1
    assert anchored is current


def test_reanchor_current_input_does_not_bind_unrelated_latest_user():
    latest = {"role": "user", "content": "latest"}
    index, anchored = reanchor_current_input(
        [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "reply"},
            latest,
        ]
    )

    assert index == -1
    assert anchored is None


def test_reanchor_current_input_restores_canonical_row_after_rewrite():
    summary = {"role": "user", "content": "compressed summary"}
    original = {
        "role": "user",
        "content": "current request",
        "api_content": "runtime context\n\ncurrent request",
        "_db_persisted": True,
    }
    messages = TurnMessageBuffer.full_snapshot([summary])

    index, anchored = reanchor_current_input(
        messages,
        content_candidates=("current request",),
        restore_message=original,
    )

    assert index == 1
    assert anchored == {
        "role": "user",
        "content": "current request",
        "api_content": "runtime context\n\ncurrent request",
    }
    assert anchored is not original
    assert messages.current_input_message is anchored
