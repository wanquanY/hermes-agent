from hermes_cli.fallback_config import get_fallback_chain, resolve_entry_api_key


def test_fallback_chain_merges_valid_routes_and_deduplicates():
    config = {
        "fallback_providers": [
            {"provider": " openrouter ", "model": " gpt-5 ", "base_url": "https://x/v1/"},
            {"provider": "incomplete"},
        ],
        "fallback_model": [
            {"provider": "OPENROUTER", "model": "GPT-5", "base_url": "https://x/v1"},
            {"provider": "anthropic", "model": "claude-opus-4.5"},
        ],
    }

    assert get_fallback_chain(config) == [
        {"provider": "openrouter", "model": "gpt-5", "base_url": "https://x/v1"},
        {"provider": "anthropic", "model": "claude-opus-4.5"},
    ]


def test_fallback_entry_key_env_is_profile_scoped(monkeypatch):
    monkeypatch.setenv("FALLBACK_TEST_KEY", "profile-secret")

    assert resolve_entry_api_key({"key_env": "FALLBACK_TEST_KEY"}) == "profile-secret"
    assert resolve_entry_api_key({"api_key_env": "FALLBACK_TEST_KEY"}) == "profile-secret"
    assert resolve_entry_api_key(
        {"api_key": "inline", "key_env": "FALLBACK_TEST_KEY"}
    ) == "inline"


def test_fallback_entry_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("MISSING_FALLBACK_TEST_KEY", raising=False)
    assert resolve_entry_api_key({"key_env": "MISSING_FALLBACK_TEST_KEY"}) is None
    assert resolve_entry_api_key(None) is None
