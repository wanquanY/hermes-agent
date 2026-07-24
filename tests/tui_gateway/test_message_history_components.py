"""Gateway history reads use the message application component."""

from __future__ import annotations

from types import SimpleNamespace

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services.message_history import (
    filter_public_conversation_history,
    load_conversation_history,
    load_runtime_conversation_history,
)


def test_load_conversation_history_delegates_to_message_component():
    captured: dict[str, object] = {}

    class _Messages:
        def all_as_conversation(self, session_id, **options):
            captured["session_id"] = session_id
            captured["options"] = options
            return [{"role": "user", "content": "hello"}]

    result = load_conversation_history(
        SimpleNamespace(messages=_Messages()),
        "session-1",
        include_ancestors=True,
        include_storage_metadata=True,
        include_inactive=True,
    )

    assert result == [{"role": "user", "content": "hello"}]
    assert captured == {
        "session_id": "session-1",
        "options": {
            "include_ancestors": True,
            "include_storage_metadata": True,
            "include_inactive": True,
        },
    }


def test_load_runtime_history_uses_explicit_private_context_projection():
    captured: dict[str, object] = {}

    class _Messages:
        def runtime_as_conversation(self, session_id, **options):
            captured["session_id"] = session_id
            captured["options"] = options
            return [{"role": "user", "content": "internal"}]

    result = load_runtime_conversation_history(
        SimpleNamespace(messages=_Messages()),
        "session-1",
        include_storage_metadata=True,
    )

    assert result == [{"role": "user", "content": "internal"}]
    assert captured == {
        "session_id": "session-1",
        "options": {
            "include_ancestors": False,
            "include_storage_metadata": True,
            "include_inactive": False,
        },
    }


def test_in_memory_public_history_filter_matches_persisted_policy():
    messages = [
        {"role": "user", "content": "visible"},
        {
            "role": "user",
            "content": "private",
            "metadata": {"transcript_visibility": "internal"},
        },
        {
            "role": "user",
            "content": (
                "You just executed tool calls but returned an empty response. "
                "Please process the tool results above and continue with the task."
            ),
        },
    ]

    assert filter_public_conversation_history(messages) == [messages[0]]


def test_message_component_merges_metadata_and_replaces_history(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create("session-1", source="test")
        store.messages.append(
            "session-1",
            "user",
            "hello",
            metadata={"client_message_id": "client-1", "first": True},
        )

        merged = store.messages.merge_metadata(
            "session-1",
            {"second": True},
            client_message_id="client-1",
        )
        store.messages.replace(
            "session-1",
            [{"role": "user", "content": "rewritten"}],
        )

        assert merged is not None
        assert merged["metadata"] == {
            "client_message_id": "client-1",
            "first": True,
            "second": True,
        }
        assert store.messages.all_as_conversation("session-1") == [
            {"role": "user", "content": "rewritten"}
        ]
    finally:
        store.close()
