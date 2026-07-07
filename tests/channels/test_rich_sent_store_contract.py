from __future__ import annotations

from channels import rich_sent_store


def test_rich_sent_store_records_and_recovers_text() -> None:
    rich_sent_store.clear()

    rich_sent_store.record("chat-1", "msg-1", "rich content")

    assert rich_sent_store.lookup("chat-1", "msg-1") == "rich content"
    assert rich_sent_store.lookup("chat-1", "missing") is None
