"""Tests for the API server bind-address startup guard.

Validates that is_network_accessible() correctly classifies addresses and
that connect() refuses to start on non-loopback without API_SERVER_KEY.
"""

import socket
from unittest.mock import patch

import pytest

from hermes_gateway.config import PlatformConfig
from channels.platforms.api_server import APIServerAdapter
from channels.platforms.base import is_network_accessible


# ---------------------------------------------------------------------------
# Unit tests: is_network_accessible()
# ---------------------------------------------------------------------------


class TestIsNetworkAccessible:
    """Direct tests for the address classification helper."""

    # -- Loopback (safe, should return False) --

    def test_ipv4_loopback(self):
        assert is_network_accessible("127.0.0.1") is False

    def test_ipv6_loopback(self):
        assert is_network_accessible("::1") is False

    def test_ipv4_mapped_loopback(self):
        # ::ffff:127.0.0.1 — Python's is_loopback returns False for mapped
        # addresses; the helper must unwrap and check ipv4_mapped.
        assert is_network_accessible("::ffff:127.0.0.1") is False

    # -- Network-accessible (should return True) --

    def test_ipv4_wildcard(self):
        assert is_network_accessible("0.0.0.0") is True

    def test_ipv6_wildcard(self):
        # This is the bypass vector that the string-based check missed.
        assert is_network_accessible("::") is True

    def test_ipv4_mapped_unspecified(self):
        assert is_network_accessible("::ffff:0.0.0.0") is True

    def test_private_ipv4(self):
        assert is_network_accessible("10.0.0.1") is True

    def test_private_ipv4_class_c(self):
        assert is_network_accessible("192.168.1.1") is True

    def test_public_ipv4(self):
        assert is_network_accessible("8.8.8.8") is True

    # -- Hostname resolution --

    def test_localhost_resolves_to_loopback(self):
        loopback_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
        ]
        with patch("channels.platforms.base._socket.getaddrinfo", return_value=loopback_result):
            assert is_network_accessible("localhost") is False

    def test_hostname_resolving_to_non_loopback(self):
        non_loopback_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
        ]
        with patch("channels.platforms.base._socket.getaddrinfo", return_value=non_loopback_result):
            assert is_network_accessible("my-server.local") is True

    def test_hostname_mixed_resolution(self):
        """If a hostname resolves to both loopback and non-loopback, it's
        network-accessible (any non-loopback address is enough)."""
        mixed_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
        ]
        with patch("channels.platforms.base._socket.getaddrinfo", return_value=mixed_result):
            assert is_network_accessible("dual-host.local") is True

    def test_dns_failure_fails_closed(self):
        """Unresolvable hostnames should require an API key (fail closed)."""
        with patch(
            "channels.platforms.base._socket.getaddrinfo",
            side_effect=socket.gaierror("Name resolution failed"),
        ):
            assert is_network_accessible("nonexistent.invalid") is True


# ---------------------------------------------------------------------------
# Integration tests: connect() startup guard
# ---------------------------------------------------------------------------


class TestConnectBindGuard:
    """Verify that connect() refuses dangerous configurations."""

    @pytest.mark.asyncio
    async def test_refuses_ipv4_wildcard_without_key(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"host": "0.0.0.0"}))
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_refuses_ipv6_wildcard_without_key(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"host": "::"}))
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_refuses_loopback_without_key(self):
        """Loopback is still an agent-control auth boundary."""
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
        assert adapter._api_key == ""
        assert is_network_accessible(adapter._host) is False
        assert await adapter.connect() is False
        assert adapter._app is None
        assert adapter._background_tasks == set()

    @pytest.mark.asyncio
    async def test_refuses_weak_key_without_partial_startup(self):
        adapter = APIServerAdapter(
            PlatformConfig(
                enabled=True,
                extra={"host": "127.0.0.1", "key": "short"},
            )
        )

        assert await adapter.connect() is False
        assert adapter._app is None
        assert adapter._background_tasks == set()

    @pytest.mark.asyncio
    async def test_allows_wildcard_with_key(self):
        """Non-loopback with a key should pass the guard."""
        adapter = APIServerAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "host": "0.0.0.0",
                    "key": "sk-test-strong-key-0123456789",
                },
            )
        )
        assert adapter._api_key_passes_startup_guard() is True
        assert is_network_accessible("0.0.0.0") is True


class TestBindMechanics:
    _KEY = "sk-test-strong-key-0123456789"

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("", 0))
            return sock.getsockname()[1]

    def _make_adapter(self, port: int) -> APIServerAdapter:
        return APIServerAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "host": "127.0.0.1",
                    "port": port,
                    "key": self._KEY,
                },
            )
        )

    @pytest.mark.asyncio
    async def test_immediate_rebind_after_disconnect(self):
        port = self._free_port()
        first = self._make_adapter(port)
        assert await first.connect() is True
        await first.disconnect()

        second = self._make_adapter(port)
        try:
            assert await second.connect() is True
        finally:
            await second.disconnect()

    @pytest.mark.asyncio
    async def test_live_listener_conflict_is_non_retryable_and_cleans_up(self):
        port = self._free_port()
        first = self._make_adapter(port)
        second = self._make_adapter(port)
        assert await first.connect() is True
        try:
            assert await second.connect() is False
            assert second._runner is None
            assert second._site is None
            assert second.is_connected is False
            assert second.has_fatal_error is True
            assert second.fatal_error_retryable is False
            assert second.fatal_error_code == "api_server_port_in_use"
            assert str(port) in (second.fatal_error_message or "")
        finally:
            await first.disconnect()
            await second.disconnect()
