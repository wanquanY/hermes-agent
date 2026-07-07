from __future__ import annotations

import os
from unittest.mock import patch

from channels.platforms.mattermost import MAX_POST_LENGTH
from channels.platforms.mattermost import MattermostAdapter
from channels.platforms.mattermost import check_mattermost_requirements
from channels.config import PlatformConfig


def test_channels_mattermost_exports_adapter_contract() -> None:
    assert MattermostAdapter.__name__ == "MattermostAdapter"
    assert MAX_POST_LENGTH == 4000
    assert isinstance(check_mattermost_requirements(), bool)


def test_channels_mattermost_formatting_contract() -> None:
    adapter = MattermostAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"url": "https://mm.example.com"},
        )
    )

    assert adapter.format_message("![cat](https://img.example.com/cat.png)") == (
        "https://img.example.com/cat.png"
    )
    assert adapter.format_message("**bold**") == "**bold**"


def test_channels_mattermost_requirements_contract() -> None:
    env = {
        "MATTERMOST_TOKEN": "token",
        "MATTERMOST_URL": "https://mm.example.com",
    }
    with patch.dict(os.environ, env):
        assert isinstance(check_mattermost_requirements(), bool)
