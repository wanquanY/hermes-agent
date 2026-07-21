"""Cross-store tests for the unified provider-credential lifecycle.

Fake credentials are assembled at runtime so key-shaped literals never land
in the repository.  These tests exercise the real dashboard endpoints against
an isolated Hermes home rather than mocking the persistence seams.
"""

import json

import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_server import _SESSION_TOKEN, app


CLIENT = TestClient(app)
HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}
OLD_ZAI_KEY = "zk-" + "a" * 24
NEW_ZAI_KEY = "zk-" + "b" * 24
OAUTH_TOKEN = "oa-" + "c" * 24


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    home = tmp_path / "credential-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.config import invalidate_env_cache

    invalidate_env_cache()
    return home


def _write_env(home, **pairs):
    home.joinpath(".env").write_text(
        "".join(f"{key}={value}\n" for key, value in pairs.items()),
        encoding="utf-8",
    )
    from hermes_cli.config import invalidate_env_cache

    invalidate_env_cache()


def _write_auth(home, pool, *, providers=None):
    home.joinpath("auth.json").write_text(
        json.dumps(
            {
                "credential_pool": pool,
                "providers": providers or {},
            }
        ),
        encoding="utf-8",
    )


def _read_auth(home):
    return json.loads(home.joinpath("auth.json").read_text(encoding="utf-8"))


def _zai_pool_fixture():
    return {
        "zai": [
            {
                "id": "env-entry",
                "label": "environment",
                "auth_type": "api_key",
                "priority": 0,
                "source": "env:ZAI_API_KEY",
                "access_token": OLD_ZAI_KEY,
            },
            {
                "id": "oauth-entry",
                "label": "OAuth",
                "auth_type": "oauth",
                "priority": 1,
                "source": "device_code",
                "access_token": OAUTH_TOKEN,
                "refresh_token": "rt-" + "d" * 16,
            },
        ]
    }


def _delete_env_key(key):
    return CLIENT.request(
        "DELETE",
        "/api/env",
        json={"key": key},
        headers=HEADERS,
    )


def test_delete_prunes_env_pool_but_preserves_oauth(hermes_home):
    _write_env(hermes_home, ZAI_API_KEY=OLD_ZAI_KEY)
    _write_auth(
        hermes_home,
        _zai_pool_fixture(),
        providers={"zai": {"access_token": OAUTH_TOKEN}},
    )

    response = _delete_env_key("ZAI_API_KEY")

    assert response.status_code == 200
    assert response.json()["pool_pruned"] == ["zai"]
    from hermes_cli.config import load_env

    assert "ZAI_API_KEY" not in load_env()
    store = _read_auth(hermes_home)
    assert [row["source"] for row in store["credential_pool"]["zai"]] == [
        "device_code"
    ]
    assert store["providers"]["zai"]["access_token"] == OAUTH_TOKEN


def test_delete_removes_empty_provider_and_survives_pool_reload(hermes_home):
    _write_env(hermes_home, ZAI_API_KEY=OLD_ZAI_KEY)
    env_row = _zai_pool_fixture()["zai"][0]
    _write_auth(hermes_home, {"zai": [env_row]})

    assert _delete_env_key("ZAI_API_KEY").status_code == 200
    assert "zai" not in _read_auth(hermes_home).get("credential_pool", {})

    from agent.credential_pool import load_pool

    assert load_pool("zai").entries() == []


def test_delete_pool_only_stale_row_is_not_a_404(hermes_home):
    _write_env(hermes_home)
    env_row = _zai_pool_fixture()["zai"][0]
    _write_auth(hermes_home, {"zai": [env_row]})

    response = _delete_env_key("ZAI_API_KEY")

    assert response.status_code == 200
    assert response.json()["found"] is True
    assert "zai" not in _read_auth(hermes_home).get("credential_pool", {})


def test_delete_unknown_key_is_a_404(hermes_home):
    _write_env(hermes_home)

    assert _delete_env_key("NEVER_SET_KEY").status_code == 404


def test_delete_does_not_touch_other_provider_rows(hermes_home):
    _write_env(hermes_home, ZAI_API_KEY=OLD_ZAI_KEY)
    pool = _zai_pool_fixture()
    pool["deepseek"] = [
        {
            "id": "deepseek-env",
            "label": "environment",
            "auth_type": "api_key",
            "priority": 0,
            "source": "env:DEEPSEEK_API_KEY",
            "access_token": "dk-" + "e" * 24,
        }
    ]
    _write_auth(hermes_home, pool)

    assert _delete_env_key("ZAI_API_KEY").status_code == 200
    assert [
        row["source"]
        for row in _read_auth(hermes_home)["credential_pool"]["deepseek"]
    ] == ["env:DEEPSEEK_API_KEY"]


def test_delete_clears_only_affected_provider_model_cache(hermes_home):
    _write_env(hermes_home, ZAI_API_KEY=OLD_ZAI_KEY)
    _write_auth(hermes_home, {"zai": [_zai_pool_fixture()["zai"][0]]})
    cache_path = hermes_home / "provider_models_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "zai": {"models": ["glm-test"], "ts": 0},
                "deepseek": {"models": ["deepseek-test"], "ts": 0},
            }
        ),
        encoding="utf-8",
    )

    assert _delete_env_key("ZAI_API_KEY").status_code == 200
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "zai" not in cache
    assert "deepseek" in cache


def test_rotation_updates_all_matching_config_mirrors_only(hermes_home):
    old = "sk-old-" + "f" * 24
    new = "sk-new-" + "g" * 24
    unrelated = "sk-other-" + "h" * 24
    _write_env(hermes_home, OPENAI_API_KEY=old)
    hermes_home.joinpath("config.yaml").write_text(
        "model:\n"
        "  provider: custom\n"
        f"  api_key: {old}\n"
        "auxiliary:\n"
        "  vision:\n"
        f"    api: {old}\n"
        "custom_providers:\n"
        "  first:\n"
        f"    api_key: {old}\n"
        "  independent:\n"
        f"    api_key: {unrelated}\n",
        encoding="utf-8",
    )

    response = CLIENT.put(
        "/api/env",
        json={"key": "OPENAI_API_KEY", "value": new},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert sorted(response.json()["config_updates"]) == [
        "auxiliary.vision.api",
        "custom_providers.first.api_key",
        "model.api_key",
    ]
    config_text = hermes_home.joinpath("config.yaml").read_text(encoding="utf-8")
    assert old not in config_text
    assert config_text.count(new) == 3
    assert unrelated in config_text


def test_delete_scrubs_matching_config_mirror(hermes_home):
    old = "sk-delete-" + "i" * 24
    _write_env(hermes_home, OPENAI_API_KEY=old)
    hermes_home.joinpath("config.yaml").write_text(
        f"model:\n  provider: custom\n  api_key: {old}\n",
        encoding="utf-8",
    )

    response = _delete_env_key("OPENAI_API_KEY")

    assert response.status_code == 200
    assert response.json()["config_scrubbed"] == ["model.api_key"]
    assert old not in hermes_home.joinpath("config.yaml").read_text(encoding="utf-8")


def test_delete_then_explicit_resave_lifts_source_suppression(hermes_home):
    _write_env(hermes_home, ZAI_API_KEY=OLD_ZAI_KEY)
    _write_auth(hermes_home, {"zai": [_zai_pool_fixture()["zai"][0]]})
    assert _delete_env_key("ZAI_API_KEY").status_code == 200

    from hermes_cli.auth import is_source_suppressed

    assert is_source_suppressed("zai", "env:ZAI_API_KEY")
    response = CLIENT.put(
        "/api/env",
        json={"key": "ZAI_API_KEY", "value": NEW_ZAI_KEY},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert not is_source_suppressed("zai", "env:ZAI_API_KEY")


def test_pool_endpoint_delete_is_sticky_across_reseed(hermes_home):
    from agent.credential_pool import load_pool
    from hermes_cli.auth import is_source_suppressed
    from hermes_cli.config import save_env_value

    key = "sk-or-" + "j" * 24
    save_env_value("OPENROUTER_API_KEY", key)
    assert [row.source for row in load_pool("openrouter").entries()] == [
        "env:OPENROUTER_API_KEY"
    ]

    response = CLIENT.delete(
        "/api/credentials/pool/openrouter/1",
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert is_source_suppressed("openrouter", "env:OPENROUTER_API_KEY")
    save_env_value("OPENROUTER_API_KEY", key)
    assert load_pool("openrouter").entries() == []


def test_pool_endpoint_explicit_readd_lifts_provider_suppressions(hermes_home):
    from agent.credential_pool import load_pool
    from hermes_cli.auth import is_source_suppressed
    from hermes_cli.config import save_env_value

    env_key = "sk-or-" + "k" * 24
    save_env_value("OPENROUTER_API_KEY", env_key)
    load_pool("openrouter")
    assert CLIENT.delete(
        "/api/credentials/pool/openrouter/1",
        headers=HEADERS,
    ).status_code == 200
    assert is_source_suppressed("openrouter", "env:OPENROUTER_API_KEY")

    response = CLIENT.post(
        "/api/credentials/pool",
        json={"provider": "openrouter", "api_key": "sk-or-" + "l" * 24},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert not is_source_suppressed("openrouter", "env:OPENROUTER_API_KEY")
    save_env_value("OPENROUTER_API_KEY", env_key)
    assert sorted(row.source for row in load_pool("openrouter").entries()) == [
        "env:OPENROUTER_API_KEY",
        "manual",
    ]


def test_pool_endpoint_manual_delete_adds_no_suppression(hermes_home):
    from hermes_cli.auth import _load_auth_store

    assert CLIENT.post(
        "/api/credentials/pool",
        json={"provider": "openrouter", "api_key": "sk-or-" + "m" * 24},
        headers=HEADERS,
    ).status_code == 200

    assert CLIENT.delete(
        "/api/credentials/pool/openrouter/1",
        headers=HEADERS,
    ).status_code == 200
    suppressed = _load_auth_store().get("suppressed_sources", {})
    assert not suppressed.get("openrouter")


def test_pool_endpoint_delete_does_not_clobber_other_provider(hermes_home):
    from agent.credential_pool import load_pool
    from hermes_cli.auth import read_credential_pool
    from hermes_cli.config import save_env_value

    assert CLIENT.post(
        "/api/credentials/pool",
        json={"provider": "anthropic", "api_key": "sk-ant-" + "n" * 24},
        headers=HEADERS,
    ).status_code == 200
    save_env_value("OPENROUTER_API_KEY", "sk-or-" + "o" * 24)
    load_pool("openrouter")

    assert CLIENT.delete(
        "/api/credentials/pool/openrouter/1",
        headers=HEADERS,
    ).status_code == 200
    assert len(read_credential_pool("anthropic")) == 1
