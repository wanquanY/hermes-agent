from __future__ import annotations

import os
from unittest.mock import patch

from channels.platforms.sms import DEFAULT_WEBHOOK_HOST
from channels.platforms.sms import MAX_SMS_LENGTH
from channels.platforms.sms import SmsAdapter
from channels.platforms.sms import check_sms_requirements
from channels.config import PlatformConfig


def test_channels_sms_exports_adapter_contract() -> None:
    assert SmsAdapter.__name__ == "SmsAdapter"
    assert SmsAdapter.MAX_MESSAGE_LENGTH == MAX_SMS_LENGTH
    assert DEFAULT_WEBHOOK_HOST == "127.0.0.1"
    assert isinstance(check_sms_requirements(), bool)


def test_channels_sms_formatting_contract() -> None:
    env = {
        "TWILIO_ACCOUNT_SID": "ACtest",
        "TWILIO_AUTH_TOKEN": "tok",
        "TWILIO_PHONE_NUMBER": "+15550001111",
    }
    with patch.dict(os.environ, env):
        adapter = SmsAdapter(PlatformConfig(enabled=True, api_key="tok"))

    assert adapter.format_message("**hello** [docs](https://example.com)") == "hello docs"
