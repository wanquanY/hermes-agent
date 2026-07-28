"""Custom provider discovery must honor the connection-owned endpoint."""

from providers import get_provider_profile
from providers.base import ProviderProfile


def test_custom_profile_forwards_runtime_base_url(monkeypatch):
    custom = get_provider_profile("custom")
    assert custom is not None
    observed: dict[str, object] = {}

    def fake_fetch_models(
        self,
        *,
        api_key=None,
        base_url=None,
        timeout=8.0,
    ):
        observed.update(
            {
                "profile": self.name,
                "api_key": api_key,
                "base_url": base_url,
                "timeout": timeout,
            }
        )
        return ["private-model"]

    monkeypatch.setattr(ProviderProfile, "fetch_models", fake_fetch_models)

    assert custom.fetch_models(
        api_key="test-key",
        base_url="https://models.example/v1/",
        timeout=3.5,
    ) == ["private-model"]
    assert observed == {
        "profile": "custom",
        "api_key": "test-key",
        "base_url": "https://models.example/v1/",
        "timeout": 3.5,
    }
