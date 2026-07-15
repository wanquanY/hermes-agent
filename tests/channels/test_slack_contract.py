from __future__ import annotations

from channels.platforms.slack import SlackAdapter
from channels.platforms.slack import _slash_user_id
from channels.platforms.slack import check_slack_requirements


def test_channels_slack_exports_adapter_contract() -> None:
    assert SlackAdapter.__name__ == "SlackAdapter"
    assert SlackAdapter.MAX_MESSAGE_LENGTH == 39000
    assert isinstance(check_slack_requirements(), bool)


def test_channels_slack_exports_slash_user_context_contract() -> None:
    assert _slash_user_id.name == "_slash_user_id"
    assert _slash_user_id.get() is None
