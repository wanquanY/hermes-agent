from types import SimpleNamespace

import pytest

from hermes_cli.credential_resolver import CredentialResolutionError
from tui_gateway.services.managed_credential_runtime import (
    refresh_managed_agent_credential,
)


def _route():
    return {
        "route": {
            "cloud_usage_policy": "direct_only",
            "connection_id": "builtin:deepseek",
            "provider_id": "deepseek",
            "model_id": "deepseek-chat",
        }
    }


class _Agent:
    model = "deepseek-chat"
    _managed_credential_ref = "ref-old"
    _managed_credential_generation = 1

    def __init__(self):
        self.switches = []

    def switch_model(self, **kwargs):
        self.switches.append(kwargs)


def test_generation_change_rebuilds_managed_provider_client(monkeypatch):
    agent = _Agent()
    monkeypatch.setattr(
        "hermes_cli.model_connections.ModelConnectionRepository.for_runtime",
        lambda: SimpleNamespace(
            get=lambda _connection_id: {
                "credential_ref": "ref-new",
            }
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.credential_resolver.runtime_credential_resolver",
        lambda: SimpleNamespace(
            status=lambda _ref: SimpleNamespace(
                configured=True,
                status="active",
                generation=2,
            )
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "new-key",
            "api_mode": "chat_completions",
            "credential_generation": 2,
            "credential_lease_id": "lease-new",
        },
    )

    changed = refresh_managed_agent_credential(agent, _route())

    assert changed is True
    assert agent.switches[0]["api_key"] == "new-key"
    assert agent._managed_credential_generation == 2


def test_revoked_credential_cannot_use_cached_provider_client(monkeypatch):
    agent = _Agent()
    monkeypatch.setattr(
        "hermes_cli.model_connections.ModelConnectionRepository.for_runtime",
        lambda: SimpleNamespace(
            get=lambda _connection_id: {
                "credential_ref": "ref-old",
            }
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.credential_resolver.runtime_credential_resolver",
        lambda: SimpleNamespace(
            status=lambda _ref: SimpleNamespace(
                configured=False,
                status="revoked",
                generation=2,
            )
        ),
    )

    with pytest.raises(CredentialResolutionError) as error:
        refresh_managed_agent_credential(agent, _route())

    assert error.value.code == "CREDENTIAL_REQUIRED"
    assert agent.switches == []
