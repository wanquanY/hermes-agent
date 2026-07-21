"""Feishu table rendering and chunk-consistency contracts."""

import asyncio
import json
from unittest.mock import AsyncMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_adapter = load_plugin_adapter("feishu")


def _build(content: str, *, prefer_post: bool = False) -> tuple[str, str]:
    instance = object.__new__(_adapter.FeishuAdapter)
    return instance._build_outbound_payload(content, prefer_post=prefer_post)


def _markdown_text(payload: str) -> str:
    document = json.loads(payload)
    elements = document["zh_cn"]["content"]
    return "\n".join(
        element.get("text", "")
        for row in elements
        for element in row
        if element.get("tag") == "md"
    )


def test_markdown_table_uses_post_not_text():
    content = "| col A | col B |\n| ----- | ----- |\n| 1 | 2 |"
    message_type, payload = _build(content)
    assert message_type == "post"
    assert "col A" in _markdown_text(payload)


def test_plain_text_stays_text_and_other_markdown_stays_post():
    assert _build("just a plain sentence")[0] == "text"
    assert _build("# hello world")[0] == "post"


def test_whole_document_preference_keeps_plain_chunk_as_post():
    message_type, payload = _build("plain continuation", prefer_post=True)
    assert message_type == "post"
    assert "plain continuation" in _markdown_text(payload)


def test_send_locks_post_type_across_chunks():
    instance = object.__new__(_adapter.FeishuAdapter)
    instance._client = object()
    instance.MAX_MESSAGE_LENGTH = 20
    instance.format_message = lambda content: content
    instance.truncate_message = lambda _content, _limit: [
        "# heading",
        "plain continuation",
    ]
    instance._feishu_send_with_retry = AsyncMock(
        return_value=type("Response", (), {"success": True, "message_id": "m1"})()
    )
    instance._response_succeeded = lambda _response: True
    instance._finalize_send_result = lambda _response, _error: type(
        "Result",
        (),
        {"success": True, "message_id": "m1", "error": None},
    )()

    asyncio.run(instance.send("chat", "# heading\n" + "plain " * 10))
    assert [call.kwargs["msg_type"] for call in instance._feishu_send_with_retry.await_args_list] == [
        "post",
        "post",
    ]
