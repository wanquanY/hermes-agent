"""Shared API listener routing for multiplexed gateway profiles."""

from types import SimpleNamespace

from channels.config import PlatformConfig
from channels.platforms.api_server import APIServerAdapter
from channels.platforms.api_server_routing import _PROFILE_REJECTED
from hermes_gateway.config import GatewayConfig


class _Request:
    def __init__(self, profile=None):
        self.match_info = {}
        if profile is not None:
            self.match_info["profile"] = profile


def _adapter(*, multiplex=True):
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"key": "a-strong-api-key-for-tests"},
        )
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=multiplex),
    )
    return adapter


def test_plain_route_resolves_to_active_profile_scope():
    assert _adapter()._resolve_request_profile(_Request()) is None


def test_known_profile_prefix_is_accepted(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", None), ("ops", None)],
    )

    assert _adapter()._resolve_request_profile(_Request("ops")) == "ops"


def test_unknown_profile_prefix_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", None), ("ops", None)],
    )

    assert (
        _adapter()._resolve_request_profile(_Request("ghost"))
        is _PROFILE_REJECTED
    )


def test_prefix_is_inert_when_multiplexing_is_disabled():
    assert _adapter(multiplex=False)._resolve_request_profile(
        _Request("ghost")
    ) is None


def test_every_native_route_can_be_mirrored_without_special_cases():
    paths = {
        path for _method, path, _handler in _adapter()._http_route_table()
    }
    mirrored = {f"/p/{{profile}}{path}" for path in paths}

    assert "/p/{profile}/v1/chat/completions" in mirrored
    assert "/p/{profile}/v1/responses" in mirrored
    assert "/p/{profile}/api/sessions" in mirrored
    assert "/p/{profile}/api/platforms/{platform}/events" in mirrored
