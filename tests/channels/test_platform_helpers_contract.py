from __future__ import annotations

import os

import httpx

from channels.platforms._http_client_limits import platform_httpx_limits
from channels.platforms.helpers import MessageDeduplicator
from channels.platforms.helpers import ThreadParticipationTracker
from channels.platforms.helpers import redact_phone
from channels.platforms.helpers import strip_markdown


def test_channels_platform_helpers_export_shared_contracts() -> None:
    dedup = MessageDeduplicator(max_size=2, ttl_seconds=60)
    assert dedup.is_duplicate("msg-1") is False
    assert dedup.is_duplicate("msg-1") is True

    assert strip_markdown("**hello** [world](https://example.test)") == "hello world"
    assert redact_phone("+8613800138000") == "+861****8000"
    assert ThreadParticipationTracker.__name__ == "ThreadParticipationTracker"


def test_channels_platform_http_limits_export_shared_contract(monkeypatch) -> None:
    monkeypatch.setitem(os.environ, "HERMES_GATEWAY_HTTPX_KEEPALIVE_EXPIRY", "3.5")
    monkeypatch.setitem(os.environ, "HERMES_GATEWAY_HTTPX_MAX_KEEPALIVE", "7")

    limits = platform_httpx_limits()

    assert isinstance(limits, httpx.Limits)
    assert limits.keepalive_expiry == 3.5
    assert limits.max_keepalive_connections == 7
