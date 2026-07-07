from __future__ import annotations

from channels.platforms.msgraph_webhook import DEFAULT_WEBHOOK_PATH
from channels.platforms.msgraph_webhook import MSGraphWebhookAdapter
from channels.platforms.msgraph_webhook import check_msgraph_webhook_requirements
from channels.platforms.webhook import DEFAULT_PORT
from channels.platforms.webhook import WebhookAdapter
from channels.platforms.webhook import _INSECURE_NO_AUTH
from channels.platforms.webhook import _is_loopback_host
from channels.platforms.webhook import check_webhook_requirements
from channels.config import PlatformConfig


def test_channels_webhook_exports_adapter_contract() -> None:
    assert WebhookAdapter.__name__ == "WebhookAdapter"
    assert DEFAULT_PORT == 8644
    assert _INSECURE_NO_AUTH == "INSECURE_NO_AUTH"
    assert isinstance(check_webhook_requirements(), bool)


def test_channels_webhook_loopback_contract() -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("0.0.0.0") is False


def test_channels_msgraph_webhook_exports_adapter_contract() -> None:
    assert MSGraphWebhookAdapter.__name__ == "MSGraphWebhookAdapter"
    assert DEFAULT_WEBHOOK_PATH == "/msgraph/webhook"
    assert isinstance(check_msgraph_webhook_requirements(), bool)


def test_channels_msgraph_webhook_path_normalization_contract() -> None:
    adapter = MSGraphWebhookAdapter(
        PlatformConfig(enabled=True, extra={"webhook_path": "msgraph/test"})
    )

    assert adapter._webhook_path == "/msgraph/test"
