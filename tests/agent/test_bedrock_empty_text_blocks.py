"""Bedrock Converse must never receive blank text content blocks."""

import pytest

from agent.bedrock_adapter import (
    _EMPTY_TEXT_PLACEHOLDER,
    _safe_text,
    convert_messages_to_converse,
)


def _all_text_blocks(messages):
    for message in messages:
        for block in message["content"]:
            if "text" in block:
                yield block["text"]
            for nested in block.get("toolResult", {}).get("content", []):
                if "text" in nested:
                    yield nested["text"]


def test_placeholder_is_not_whitespace():
    assert _EMPTY_TEXT_PLACEHOLDER.strip()


@pytest.mark.parametrize("value", ["", "   ", "\n\n", "\t", None])
def test_safe_text_replaces_blank_inputs(value):
    assert _safe_text(value).strip()


def test_conversion_filters_blank_history_and_tool_results():
    source = [
        {"role": "system", "content": [{"type": "text", "text": "   "}]},
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "shell", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "\n"},
        {"role": "assistant", "content": None},
    ]

    _system, converted = convert_messages_to_converse(source)

    assert all(text.strip() for text in _all_text_blocks(converted))


def test_real_content_survives_next_to_blank_siblings():
    _system, converted = convert_messages_to_converse(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": " "},
                    {"type": "text", "text": "real question"},
                ],
            }
        ]
    )

    texts = list(_all_text_blocks(converted))
    assert "real question" in texts
    assert all(text.strip() for text in texts)
