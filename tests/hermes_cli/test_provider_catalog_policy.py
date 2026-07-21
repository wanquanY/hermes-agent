from __future__ import annotations

import pytest

from hermes_cli.config import (
    apply_configured_request_headers,
    get_compatible_custom_providers,
    is_provider_enabled,
    normalize_extra_headers,
)
from hermes_cli.custom_provider_inventory import (
    build_keyed_provider_rows,
    build_saved_custom_provider_rows,
    save_discovered_models_to_config,
)
from hermes_cli.inventory import ConfigContext, _append_unconfigured_rows
from hermes_cli.model_switch import list_authenticated_providers


@pytest.mark.parametrize("value", [False, "false", "0", "no", "off"])
def test_provider_enabled_explicit_false_values(value):
    assert is_provider_enabled({"enabled": value}) is False


@pytest.mark.parametrize("value", [None, {}, {"enabled": True}, {"enabled": "yes"}])
def test_provider_enabled_defaults_open(value):
    assert is_provider_enabled(value) is True


def test_compatibility_view_omits_disabled_provider():
    providers = get_compatible_custom_providers(
        {
            "providers": {
                "live": {"base_url": "https://live.example/v1"},
                "paused": {
                    "base_url": "https://paused.example/v1",
                    "enabled": False,
                },
            }
        }
    )
    assert [provider["provider_key"] for provider in providers] == ["live"]


def test_excluded_provider_is_filtered_before_catalog_probe(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "agent.models_dev.fetch_models_dev",
        lambda **_kwargs: {
            "openrouter": {"env": ["OPENROUTER_API_KEY"], "models": {}},
        },
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids",
        lambda provider: calls.append(provider) or ["model-a"],
    )

    rows = list_authenticated_providers(excluded_providers=["openrouter"])

    assert all(row["slug"] != "openrouter" for row in rows)
    assert "openrouter" not in calls


def test_disabled_user_provider_is_not_probed(monkeypatch):
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda **_kwargs: {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setattr(
        "hermes_cli.models.fetch_api_models",
        lambda *_args, **_kwargs: pytest.fail("disabled provider was probed"),
    )

    rows = list_authenticated_providers(
        user_providers={
            "paused": {
                "enabled": False,
                "base_url": "https://paused.example/v1",
                "api_key": "secret",
                "models": ["model-a"],
            }
        },
        custom_providers=[],
    )

    assert all(row["slug"] != "paused" for row in rows)


def test_runtime_rejects_disabled_builtin_case_insensitively(monkeypatch):
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "load_config",
        lambda: {"providers": {"OpenRouter": {"enabled": False}}},
    )

    with pytest.raises(ValueError, match="providers.OpenRouter.enabled: false"):
        runtime_provider.resolve_runtime_provider(requested="openrouter")


def test_named_custom_runtime_propagates_extra_headers(monkeypatch):
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "load_config",
        lambda: {
            "providers": {
                "proxy": {
                    "base_url": "https://proxy.example/v1",
                    "api_key": "key",
                    "extra_headers": {"X-Proxy-Token": "secret"},
                }
            }
        },
    )

    resolved = runtime_provider.resolve_runtime_provider(requested="proxy")

    assert resolved["extra_headers"] == {"X-Proxy-Token": "secret"}


@pytest.mark.parametrize(
    ("requested_key", "expected_key"),
    [("sk-new", "sk-new"), ("", "sk-stored")],
)
def test_dashboard_provider_key_precedence(monkeypatch, requested_key, expected_key):
    from hermes_cli import web_server

    cfg = {
        "model": {"provider": "openrouter", "default": "old"},
        "providers": {
            "acme": {
                "base_url": "https://acme.example/v1",
                "api_key": "sk-stored",
            }
        },
    }
    monkeypatch.setattr(web_server, "load_config", lambda: cfg)
    monkeypatch.setattr(web_server, "save_config", lambda _cfg: None)
    monkeypatch.setattr(
        web_server,
        "_normalize_main_model_assignment",
        lambda provider, model: (provider, model),
    )

    web_server._apply_model_assignment_sync(
        "main", "acme", "model-a", "", "", requested_key
    )

    assert cfg["model"]["api_key"] == expected_key
    assert cfg["model"]["base_url"] == "https://acme.example/v1"


def test_unconfigured_inventory_does_not_restore_excluded_or_disabled():
    ctx = ConfigContext(
        current_provider="",
        current_model="",
        current_base_url="",
        user_providers={"openrouter": {"enabled": False}},
        custom_providers=[],
        excluded_providers=("anthropic",),
    )

    slugs = {row["slug"] for row in _append_unconfigured_rows([], ctx)}

    assert "openrouter" not in slugs
    assert "anthropic" not in slugs


def test_same_endpoint_distinct_provider_names_remain_distinct(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.models.fetch_api_models", lambda *_args, **_kwargs: None
    )
    seen: set[str] = set()
    rows = build_saved_custom_provider_rows(
        [
            {
                "name": "Cerebras",
                "base_url": "https://proxy.example/v1",
                "models": ["cerebras-model"],
            },
            {
                "name": "Groq",
                "base_url": "https://proxy.example/v1",
                "models": ["groq-model"],
            },
        ],
        current_provider="",
        current_model="",
        current_base_url="",
        seen_slugs=seen,
        emitted_provider_pairs=set(),
        builtin_endpoints=set(),
        probe_all=False,
        probe_current=False,
        excluded_names=set(),
    )

    assert [(row["name"], row["models"]) for row in rows] == [
        ("Cerebras", ["cerebras-model"]),
        ("Groq", ["groq-model"]),
    ]


def test_keyed_per_model_entries_group_by_transport_and_credential():
    seen: set[str] = set()
    rows, pairs = build_keyed_provider_rows(
        {
            "palantir-opus": {
                "name": "Palantir Claude 4.7 Opus",
                "base_url": "https://proxy.example/v1",
                "key_env": "PALANTIR_KEY",
                "api_mode": "anthropic_messages",
                "model": "claude-opus-4-7",
            },
            "palantir-sonnet": {
                "name": "Palantir Claude 4.7 Sonnet",
                "base_url": "https://proxy.example/v1",
                "key_env": "PALANTIR_KEY",
                "api_mode": "anthropic_messages",
                "model": "claude-sonnet-4-7",
            },
            "palantir-openai": {
                "name": "Palantir OpenAI",
                "base_url": "https://proxy.example/v1",
                "key_env": "PALANTIR_KEY",
                "api_mode": "chat_completions",
                "model": "gpt-5.4",
            },
        },
        current_provider="",
        current_base_url="",
        seen_slugs=seen,
        curated_models={},
        probe_all=False,
        probe_current=False,
        excluded_names=set(),
    )

    assert [(row["name"], row["models"]) for row in rows] == [
        ("Palantir Claude", ["claude-opus-4-7", "claude-sonnet-4-7"]),
        ("Palantir OpenAI", ["gpt-5.4"]),
    ]
    assert ("palantir claude 4.7 opus", "https://proxy.example/v1") in pairs


def test_only_current_custom_provider_is_probed(monkeypatch):
    calls: list[str] = []

    def fake_fetch(_api_key, base_url, **_kwargs):
        calls.append(base_url)
        return ["live-model"]

    monkeypatch.setattr("hermes_cli.models.fetch_api_models", fake_fetch)
    rows = build_saved_custom_provider_rows(
        [
            {"name": "Current", "base_url": "https://current.example/v1"},
            {"name": "Offline", "base_url": "https://offline.example/v1"},
        ],
        current_provider="custom",
        current_model="",
        current_base_url="https://current.example/v1",
        seen_slugs=set(),
        emitted_provider_pairs=set(),
        builtin_endpoints=set(),
        probe_all=False,
        probe_current=True,
        excluded_names=set(),
    )

    assert calls == ["https://current.example/v1"]
    assert rows[0]["models"] == ["live-model"]
    assert rows[1]["models"] == []


@pytest.mark.parametrize(
    "metadata_models",
    [
        {"configured": {"context_length": 8192}},
        [{"id": "configured", "context_length": 8192}],
    ],
)
def test_discovered_model_cache_preserves_metadata(monkeypatch, metadata_models):
    cfg = {
        "custom_providers": [
            {
                "name": "Metadata",
                "base_url": "https://metadata.example/v1",
                "models": metadata_models,
            }
        ]
    }
    writes: list[dict] = []
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.save_config", writes.append)

    save_discovered_models_to_config(
        "https://metadata.example/v1", ["configured", "discovered"]
    )

    assert writes == []
    assert cfg["custom_providers"][0]["models"] == metadata_models


def test_configured_header_precedence_and_normalization():
    client_kwargs = {"default_headers": {"User-Agent": "sdk", "X-SDK": "1"}}
    cfg = {
        "model": {
            "default_headers": {"User-Agent": "global", "X-Global": 2},
            "extra_headers": {"X-Alias": "yes"},
        },
        "providers": {
            "proxy": {
                "base_url": "https://proxy.example/v1",
                "extra_headers": {"User-Agent": "provider", "X-Secret": "token"},
            }
        },
    }

    apply_configured_request_headers(
        client_kwargs, "https://proxy.example/v1/", config=cfg
    )

    assert client_kwargs["default_headers"] == {
        "User-Agent": "provider",
        "X-SDK": "1",
        "X-Global": "2",
        "X-Alias": "yes",
        "X-Secret": "token",
    }
    assert normalize_extra_headers({"A": 1, "B": None}) == {"A": "1"}


def test_extra_headers_are_part_of_custom_provider_identity(monkeypatch):
    calls: list[dict] = []

    def fake_fetch(_api_key, _base_url, **kwargs):
        calls.append(kwargs["headers"])
        return [kwargs["headers"]["X-Tenant"]]

    monkeypatch.setattr("hermes_cli.models.fetch_api_models", fake_fetch)
    rows = build_saved_custom_provider_rows(
        [
            {
                "name": "Proxy",
                "base_url": "https://proxy.example/v1",
                "api_key": "key",
                "extra_headers": {"X-Tenant": "a"},
            },
            {
                "name": "Proxy",
                "base_url": "https://proxy.example/v1",
                "api_key": "key",
                "extra_headers": {"X-Tenant": "b"},
            },
        ],
        current_provider="",
        current_model="",
        current_base_url="",
        seen_slugs=set(),
        emitted_provider_pairs=set(),
        builtin_endpoints=set(),
        probe_all=True,
        probe_current=False,
        excluded_names=set(),
    )

    assert calls == [{"X-Tenant": "a"}, {"X-Tenant": "b"}]
    assert [row["models"] for row in rows] == [["a"], ["b"]]


def test_auxiliary_openai_proxy_applies_endpoint_headers(monkeypatch):
    from agent import auxiliary_client
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {
            "providers": {
                "proxy": {
                    "base_url": "https://proxy.example/v1",
                    "extra_headers": {"X-Proxy-Token": "secret"},
                }
            }
        },
    )
    monkeypatch.setattr(
        "agent.process_bootstrap.build_provider_http_client", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_load_openai_cls",
        lambda: lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(auxiliary_client, "_attach_dovie_attribution", lambda client: client)

    client = auxiliary_client._OpenAIProxy()(
        api_key="key", base_url="https://proxy.example/v1"
    )

    assert client["default_headers"]["X-Proxy-Token"] == "secret"
