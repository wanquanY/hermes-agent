from __future__ import annotations

import base64
import json
import time

from tui_gateway import server

import tui_gateway.methods.codex  # noqa: F401


def _call(method: str, params: dict):
    return server._methods[method]("rid-1", params)


def _jwt(exp_epoch: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_epoch}).encode("utf-8")
    ).rstrip(b"=").decode("utf-8")
    return f"h.{payload}.s"


def test_codex_auth_status_logged_in(tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    access_token = _jwt(int(time.time()) + 3600)
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "account_id": "acct_abcdef1234",
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": "rt",
                },
            }
        )
    )

    resp = _call("codex.auth.status", {"codex_home": str(codex_home)})

    result = resp["result"]
    assert result["state"] == "logged_in"
    assert result["auth_mode"] == "chatgpt"
    assert result["expires_at"]
    assert result["account_id"] == "1234"


def test_codex_auth_status_missing(tmp_path):
    codex_home = tmp_path / "codex"

    resp = _call("codex.auth.status", {"codex_home": str(codex_home)})

    assert resp["result"] == {
        "state": "missing",
        "auth_mode": None,
        "expires_at": None,
        "account_id": None,
    }


def test_codex_auth_login_start(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex"

    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_request",
        lambda client_id: {
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.openai.com/codex/device",
            "device_auth_id": "dev-123",
            "poll_interval": 5,
            "expires_in": 900,
        },
    )

    resp = _call(
        "codex.auth.login.start",
        {"codex_home": str(codex_home), "channel": "device_code"},
    )

    assert resp["result"]["user_code"] == "ABCD-EFGH"
    assert resp["result"]["device_auth_id"] == "dev-123"


def test_codex_auth_login_poll_pending(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex"

    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_poll_once",
        lambda device_auth_id, client_id, user_code=None: {"state": "pending"},
    )

    resp = _call(
        "codex.auth.login.poll",
        {"codex_home": str(codex_home), "device_auth_id": "dev-123"},
    )

    assert resp["result"] == {"state": "pending"}
    assert not (codex_home / "auth.json").exists()


def test_codex_auth_login_poll_success_saves_to_codex_home(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex"
    access_token = _jwt(int(time.time()) + 3600)

    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_poll_once",
        lambda device_auth_id, client_id, user_code=None: {
            "state": "logged_in",
            "tokens": {
                "access_token": access_token,
                "refresh_token": "rt",
            },
            "last_refresh": "2026-07-04T00:00:00Z",
        },
    )

    resp = _call(
        "codex.auth.login.poll",
        {"codex_home": str(codex_home), "device_auth_id": "dev-123"},
    )

    assert resp["result"]["state"] == "logged_in"
    payload = json.loads((codex_home / "auth.json").read_text())
    assert payload["auth_mode"] == "chatgpt"
    assert payload["tokens"]["access_token"] == access_token


def test_codex_auth_logout_removes_only_target_auth(tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    auth_path.write_text(json.dumps({"tokens": {"access_token": "at"}}))

    resp = _call("codex.auth.logout", {"codex_home": str(codex_home)})

    assert resp["result"] == {"state": "missing"}
    assert not auth_path.exists()


def test_codex_auth_import_cli(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex"

    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens",
        lambda: {"access_token": "cli-at", "refresh_token": "cli-rt"},
    )

    resp = _call("codex.auth.import_cli", {"codex_home": str(codex_home)})

    assert resp["result"] == {"state": "logged_in"}
    payload = json.loads((codex_home / "auth.json").read_text())
    assert payload["tokens"]["refresh_token"] == "cli-rt"


def test_codex_auth_import_cli_not_found(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex"
    monkeypatch.setattr("hermes_cli.auth._import_codex_cli_tokens", lambda: None)

    resp = _call("codex.auth.import_cli", {"codex_home": str(codex_home)})

    assert resp["result"] == {"state": "not_found"}
    assert not (codex_home / "auth.json").exists()


def test_codex_auth_rejects_missing_codex_home():
    resp = _call("codex.auth.status", {})

    assert resp["error"]["code"] == 4002
    assert "codex_home is required" in resp["error"]["message"]


def test_codex_auth_rejects_relative_codex_home():
    resp = _call("codex.auth.login.start", {"codex_home": "relative/path"})

    assert resp["error"]["code"] == 4002
    assert "absolute path" in resp["error"]["message"]


def test_codex_auth_poll_surfaces_auth_error(monkeypatch, tmp_path):
    from hermes_cli.auth import AuthError

    codex_home = tmp_path / "codex"

    def _boom(device_auth_id, client_id, user_code=None):
        raise AuthError("expired", provider="openai-codex", code="expired_token")

    monkeypatch.setattr("hermes_cli.auth._codex_device_code_poll_once", _boom)

    resp = _call(
        "codex.auth.login.poll",
        {"codex_home": str(codex_home), "device_auth_id": "dev-123"},
    )

    assert resp["error"]["code"] == 4003
    assert resp["error"]["data"]["code"] == "expired_token"
