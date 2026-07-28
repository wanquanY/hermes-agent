"""Adapter-registry routing contracts for reconnects and named profiles."""

from types import SimpleNamespace

from channels.platforms.base import BasePlatformAdapter, SendResult
from channels.session_identity import SessionSource
from hermes_gateway.config import Platform, PlatformConfig
from hermes_gateway.runner import GatewayRunner


class _RoutingAdapter(BasePlatformAdapter):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="sent")

    async def get_chat_info(self, chat_id):
        return {}


def _adapter(platform=Platform.DISCORD):
    return _RoutingAdapter(
        config=PlatformConfig(enabled=True, token="test"),
        platform=platform,
    )


def _runner(default_adapter, profile_adapters=None):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {default_adapter.platform: default_adapter}
    runner._profile_adapters = profile_adapters or {}
    return runner


def _source(*, profile=None, platform=Platform.DISCORD):
    return SessionSource(
        platform=platform,
        chat_id="channel-1",
        chat_type="channel",
        profile=profile,
    )


def test_adapter_for_source_resolves_default_registry():
    default_adapter = _adapter()
    runner = _runner(default_adapter)

    assert runner._adapter_for_source(_source()) is default_adapter
    assert runner._adapter_for_source(_source(profile="default")) is default_adapter
    assert runner._adapter_for_source(_source(profile="main")) is None


def test_adapter_for_source_resolves_secondary_profile_registry():
    default_adapter = _adapter()
    reviewer_adapter = _adapter()
    runner = _runner(
        default_adapter,
        profile_adapters={"reviewer": {Platform.DISCORD: reviewer_adapter}},
    )

    assert runner._adapter_for_source(_source(profile="reviewer")) is reviewer_adapter


def test_missing_secondary_profile_adapter_fails_closed():
    default_adapter = _adapter()
    runner = _runner(default_adapter)

    assert runner._adapter_for_source(_source(profile="missing")) is None


def test_final_delivery_uses_live_same_platform_replacement():
    stale_adapter = _adapter()
    live_adapter = _adapter()
    stale_adapter.gateway_runner = SimpleNamespace(
        _adapter_for_source=lambda source: live_adapter
    )

    assert stale_adapter._final_delivery_adapter(_source()) is live_adapter


def test_final_delivery_never_crosses_platform_boundary():
    stale_adapter = _adapter()
    wrong_platform_adapter = _adapter(Platform.TELEGRAM)
    stale_adapter.gateway_runner = SimpleNamespace(
        _adapter_for_source=lambda source: wrong_platform_adapter
    )

    assert stale_adapter._final_delivery_adapter(_source()) is stale_adapter
