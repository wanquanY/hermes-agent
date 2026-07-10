"""Gateway history reads use the message application component."""

from __future__ import annotations

from types import SimpleNamespace

from tui_gateway.services.message_history import load_conversation_history


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
