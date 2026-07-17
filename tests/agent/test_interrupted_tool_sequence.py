from __future__ import annotations

from agent.message_sanitization import close_interrupted_tool_sequence


def _tool_tail():
    return [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]


def test_close_interrupted_tool_sequence_appends_nonempty_assistant_boundary():
    messages = _tool_tail()

    changed = close_interrupted_tool_sequence(messages)

    assert changed is True
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Operation interrupted."
    assert messages[-1]["metadata"]["synthetic_kind"] == "interrupted_tool_sequence_close"


def test_close_interrupted_tool_sequence_uses_visible_interrupt_text():
    messages = _tool_tail()

    close_interrupted_tool_sequence(messages, "Stopped during retry.")

    assert messages[-1]["content"] == "Stopped during retry."


def test_close_interrupted_tool_sequence_is_idempotent_after_close():
    messages = _tool_tail()
    assert close_interrupted_tool_sequence(messages) is True

    assert close_interrupted_tool_sequence(messages) is False
    assert sum(
        m.get("metadata", {}).get("synthetic_kind") == "interrupted_tool_sequence_close"
        for m in messages
    ) == 1


def test_close_interrupted_tool_sequence_does_not_change_non_tool_tail():
    messages = [{"role": "assistant", "content": "done"}]

    assert close_interrupted_tool_sequence(messages) is False
    assert messages == [{"role": "assistant", "content": "done"}]
