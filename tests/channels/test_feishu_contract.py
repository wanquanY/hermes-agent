from __future__ import annotations

from types import SimpleNamespace

from channels.platforms.feishu import FeishuAdapter
from channels.platforms.feishu import FeishuBatchState
from channels.platforms.feishu import normalize_feishu_message
from channels.platforms.feishu import check_feishu_requirements
from channels.platforms.feishu_comment import parse_drive_comment_event
from channels.platforms.feishu_comment_rules import CommentsConfig
from channels.platforms.feishu_comment_rules import ResolvedCommentRule
from channels.platforms.feishu_comment_rules import has_wiki_keys
from channels.platforms.feishu_comment_rules import is_user_allowed


def test_channels_feishu_exports_adapter_contract() -> None:
    assert FeishuAdapter.__name__ == "FeishuAdapter"
    assert FeishuAdapter.MAX_MESSAGE_LENGTH == 8000
    assert FeishuBatchState().events == {}
    assert callable(check_feishu_requirements)


def test_channels_feishu_normalizes_plain_text_contract() -> None:
    normalized = normalize_feishu_message(
        message_type="text",
        raw_content='{"text": "hello"}',
    )

    assert normalized.text_content == "hello"


def test_channels_feishu_comment_event_contract() -> None:
    payload = SimpleNamespace(
        event={
            "event_id": "evt-1",
            "comment_id": "c-1",
            "reply_id": "",
            "is_mentioned": True,
            "timestamp": "123",
            "notice_meta": {
                "notice_type": "comment_add",
                "file_token": "doc-token",
                "file_type": "docx",
                "from_user_id": {"open_id": "ou-1"},
                "to_user_id": {"open_id": "ou-bot"},
            },
        }
    )

    parsed = parse_drive_comment_event(payload)

    assert parsed is not None
    assert parsed["file_token"] == "doc-token"
    assert parsed["from_open_id"] == "ou-1"


def test_channels_feishu_comment_rules_contract() -> None:
    cfg = CommentsConfig()
    rule = ResolvedCommentRule(
        enabled=True,
        policy="allowlist",
        allow_from=frozenset({"ou-1"}),
        match_source="test",
    )

    assert has_wiki_keys(cfg) is False
    assert is_user_allowed(rule, "ou-1") is True
    assert is_user_allowed(rule, "ou-2") is False
