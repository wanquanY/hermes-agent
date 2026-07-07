from __future__ import annotations

import os
from unittest.mock import patch

from channels.platforms.email import EmailAdapter
from channels.platforms.email import MAX_MESSAGE_LENGTH
from channels.platforms.email import _decode_header_value
from channels.platforms.email import _extract_email_address
from channels.platforms.email import _strip_html
from channels.platforms.email import check_email_requirements
from channels.config import PlatformConfig


def test_channels_email_exports_adapter_contract() -> None:
    assert EmailAdapter.__name__ == "EmailAdapter"
    assert MAX_MESSAGE_LENGTH == 50_000
    assert isinstance(check_email_requirements(), bool)


def test_channels_email_parsing_contract() -> None:
    assert _decode_header_value("=?utf-8?B?TWVyaGFiYQ==?=") == "Merhaba"
    assert _extract_email_address("John Doe <john@example.com>") == "john@example.com"
    assert _strip_html("<p>a &amp; b</p>") == "a & b"


def test_channels_email_adapter_reads_environment_contract() -> None:
    env = {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_SMTP_HOST": "smtp.test.com",
    }
    with patch.dict(os.environ, env):
        adapter = EmailAdapter(PlatformConfig(enabled=True))

    assert adapter._address == "hermes@test.com"
    assert adapter._imap_host == "imap.test.com"
    assert adapter._smtp_host == "smtp.test.com"
