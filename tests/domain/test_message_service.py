from __future__ import annotations

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_tool_call_only_assistant_message_does_not_require_content(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.sessions.create("session-1", source="cli")
        tool_calls = [
            {
                "id": "call-1",
                "function": {"name": "web_search", "arguments": "{}"},
            }
        ]

        store.messages.append(
            "session-1",
            role="assistant",
            tool_calls=tool_calls,
        )

        messages = store.messages.list("session-1")
        assert len(messages) == 1
        assert messages[0]["content"] is None
        assert messages[0]["tool_calls"] == tool_calls
    finally:
        store.close()
