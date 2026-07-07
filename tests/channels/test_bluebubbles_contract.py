from __future__ import annotations

import os
from unittest.mock import patch

from channels.platforms.bluebubbles import DEFAULT_WEBHOOK_PATH
from channels.platforms.bluebubbles import MAX_TEXT_LENGTH
from channels.platforms.bluebubbles import BlueBubblesAdapter
from channels.platforms.bluebubbles import _normalize_server_url
from channels.platforms.bluebubbles import check_bluebubbles_requirements
from channels.config import PlatformConfig


def test_channels_bluebubbles_exports_adapter_contract() -> None:
    assert BlueBubblesAdapter.__name__ == "BlueBubblesAdapter"
    assert BlueBubblesAdapter.MAX_MESSAGE_LENGTH == MAX_TEXT_LENGTH
    assert DEFAULT_WEBHOOK_PATH == "/bluebubbles-webhook"
    assert isinstance(check_bluebubbles_requirements(), bool)


def test_channels_bluebubbles_url_and_format_contract() -> None:
    assert _normalize_server_url("localhost:1234/") == "http://localhost:1234"

    env = {
        "BLUEBUBBLES_SERVER_URL": "localhost:1234",
        "BLUEBUBBLES_PASSWORD": "secret",
    }
    with patch.dict(os.environ, env):
        adapter = BlueBubblesAdapter(
            PlatformConfig(
                enabled=True,
                extra={"server_url": "localhost:1234", "password": "secret"},
            )
        )

    assert adapter.server_url == "http://localhost:1234"
    assert adapter.format_message("**Hello** `world`") == "Hello world"
