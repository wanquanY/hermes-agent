"""Regression tests for bash-compatible export-prefixed dotenv entries."""

import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_server import _SESSION_TOKEN, app


CLIENT = TestClient(app)
HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}
OLD_TOKEN = "ghp_" + "A" * 36
NEW_TOKEN = "ghp_" + "B" * 36


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    home = tmp_path / "export-env-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.config import invalidate_env_cache

    invalidate_env_cache()
    return home


def _write_env(home, text):
    home.joinpath(".env").write_text(text, encoding="utf-8")
    from hermes_cli.config import invalidate_env_cache

    invalidate_env_cache()


def test_export_prefixed_token_can_be_removed(hermes_home):
    _write_env(hermes_home, f"export GITHUB_TOKEN={OLD_TOKEN}\n")

    response = CLIENT.request(
        "DELETE",
        "/api/env",
        json={"key": "GITHUB_TOKEN"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert OLD_TOKEN not in hermes_home.joinpath(".env").read_text(encoding="utf-8")


def test_save_canonicalizes_plain_and_export_duplicates(hermes_home):
    _write_env(
        hermes_home,
        f"export GITHUB_TOKEN={OLD_TOKEN}\nGITHUB_TOKEN={OLD_TOKEN}\n",
    )

    response = CLIENT.put(
        "/api/env",
        json={"key": "GITHUB_TOKEN", "value": NEW_TOKEN},
        headers=HEADERS,
    )

    assert response.status_code == 200
    env_text = hermes_home.joinpath(".env").read_text(encoding="utf-8")
    assert OLD_TOKEN not in env_text
    assert env_text.count("GITHUB_TOKEN=") == 1
    assert f"GITHUB_TOKEN={NEW_TOKEN}" in env_text


def test_plain_line_save_and_remove_still_work(hermes_home):
    from hermes_cli.config import load_env, remove_env_value, save_env_value

    save_env_value("GITHUB_TOKEN", OLD_TOKEN)
    assert load_env()["GITHUB_TOKEN"] == OLD_TOKEN
    save_env_value("GITHUB_TOKEN", NEW_TOKEN)
    assert remove_env_value("GITHUB_TOKEN") is True
    assert "GITHUB_TOKEN" not in load_env()


def test_commented_export_is_not_a_live_assignment(hermes_home):
    _write_env(
        hermes_home,
        f"# export GITHUB_TOKEN={OLD_TOKEN}\nOTHER_KEY=value\n",
    )

    response = CLIENT.request(
        "DELETE",
        "/api/env",
        json={"key": "GITHUB_TOKEN"},
        headers=HEADERS,
    )

    assert response.status_code == 404
    env_text = hermes_home.joinpath(".env").read_text(encoding="utf-8")
    assert "# export GITHUB_TOKEN=" in env_text
    assert "OTHER_KEY=value" in env_text
