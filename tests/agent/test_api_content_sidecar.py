from __future__ import annotations

from agent.api_content import (
    api_content_for_storage,
    compose_user_api_content,
    drop_stale_api_content,
    substitute_api_content,
)
from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_compose_user_api_content_keeps_clean_prefix_and_ordered_context():
    result = compose_user_api_content(
        "clean request",
        memory_context="remember this",
        plugin_context="plugin facts",
    )

    assert result is not None
    assert result.startswith("clean request\n\n<memory-context>\n")
    assert "remember this\n</memory-context>\n\nplugin facts" in result


def test_compose_without_context_returns_original_object_value():
    assert compose_user_api_content("clean request") == "clean request"
    assert compose_user_api_content([{"type": "text", "text": "x"}]) is None


def test_substitute_api_content_uses_copy_sidecar_and_removes_internal_key():
    message = {
        "role": "user",
        "content": "clean",
        "api_content": "provider wire bytes",
    }

    assert substitute_api_content(message) == "provider wire bytes"
    assert message == {"role": "user", "content": "provider wire bytes"}


def test_storage_sidecar_captures_read_model_normalization():
    assert (
        api_content_for_storage(
            role="assistant",
            content=" exact response ",
            explicit_sidecar=None,
        )
        == " exact response "
    )
    fenced = "clean\n<memory-context>private</memory-context>"
    assert (
        api_content_for_storage(
            role="user",
            content=fenced,
            explicit_sidecar=None,
        )
        == fenced
    )
    assert (
        api_content_for_storage(
            role="tool",
            content=" exact tool result ",
            explicit_sidecar=None,
        )
        is None
    )


def test_stale_sidecar_is_dropped_after_clean_content_changes():
    message = {"role": "user", "content": "new", "api_content": "old wire"}
    drop_stale_api_content(message)
    assert message == {"role": "user", "content": "new"}


def test_chat_completions_transport_strips_storage_sidecar_without_mutation():
    original = {
        "role": "assistant",
        "content": "provider wire bytes",
        "api_content": "provider wire bytes",
    }
    converted = ChatCompletionsTransport().convert_messages([original])

    assert converted == [{"role": "assistant", "content": "provider wire bytes"}]
    assert original["api_content"] == "provider wire bytes"


def test_message_service_round_trips_and_safely_backfills_sidecar(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.messages.append(
            "session-1",
            "user",
            "clean request",
            api_content="wire version one",
            conversation_message_id="message-1",
        )
        assert store.messages.all_as_conversation("session-1") == [
            {
                "role": "user",
                "content": "clean request",
                "api_content": "wire version one",
                "conversation_message_id": "message-1",
            }
        ]
        assert (
            store.messages.page_as_conversation("session-1")["messages"][0][
                "api_content"
            ]
            == "wire version one"
        )

        assert (
            store.messages.set_current_user_api_content(
                "session-1",
                content="wrong clean request",
                api_content="must not overwrite",
                conversation_message_id="message-1",
            )
            == 0
        )
        assert (
            store.messages.set_current_user_api_content(
                "session-1",
                content="clean request",
                api_content="wire version two",
                conversation_message_id="message-1",
            )
            == 1
        )
        assert store.messages.all_as_conversation("session-1")[0]["api_content"] == (
            "wire version two"
        )

        store.messages.replace(
            "session-1",
            [
                {
                    "role": "assistant",
                    "content": "clean answer",
                    "api_content": "provider-side answer",
                }
            ],
        )
        assert store.messages.all_as_conversation("session-1") == [
            {
                "role": "assistant",
                "content": "clean answer",
                "api_content": "provider-side answer",
            }
        ]
    finally:
        store.close()
