from __future__ import annotations

import json

import pytest

from hermes_cli.credential_resolver import CredentialStatus, ResolvedCredential
from hermes_cli import managed_oauth


class _Repository:
    def __init__(self, connection: dict):
        self.connection = connection

    def get(self, connection_id: str):
        if connection_id == self.connection["connection_id"]:
            return dict(self.connection)
        return None


class _Resolver:
    def __init__(self):
        self.updates: list[dict] = []

    def update_oauth(self, credential_ref: str, **kwargs):
        self.updates.append({"credential_ref": credential_ref, **kwargs})
        return CredentialStatus(
            configured=False,
            status="staged",
            generation=int(kwargs["expected_generation"]) + 1,
            secret_kind="oauth_bundle",
        )


def _install_connection(monkeypatch: pytest.MonkeyPatch, provider_id: str):
    connection = {
        "connection_id": f"builtin:{provider_id}",
        "provider_id": provider_id,
        "credential_ref": "dovie-secure://model-credentials/cred_oauth",
        "enabled": False,
        "revision": 1,
    }
    repository = _Repository(connection)
    monkeypatch.setattr(
        managed_oauth.ModelConnectionRepository,
        "for_runtime",
        classmethod(lambda cls: repository),
    )
    return connection


def test_managed_anthropic_start_sinks_tokens_to_broker(
    monkeypatch: pytest.MonkeyPatch,
):
    connection = _install_connection(monkeypatch, "anthropic")
    resolver = _Resolver()
    monkeypatch.setattr(managed_oauth, "runtime_credential_resolver", lambda: resolver)

    from hermes_cli import web_server

    captured = {}

    def fake_start(sink):
        captured["status"] = sink(
            {
                "provider_id": "anthropic",
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
            }
        )
        return {
            "session_id": "oauth-session",
            "flow": "pkce",
            "auth_url": "https://example.invalid/oauth",
        }

    monkeypatch.setattr(web_server, "_start_anthropic_pkce", fake_start)

    result = managed_oauth.start_managed_oauth(
        provider_id="anthropic",
        connection_id=connection["connection_id"],
        credential_ref=connection["credential_ref"],
        expected_generation=4,
    )

    assert result["status"] == "pending"
    assert result["session_id"] == "oauth-session"
    assert "oauth-access" not in json.dumps(result)
    assert captured["status"]["generation"] == 5
    assert resolver.updates == [
        {
            "credential_ref": connection["credential_ref"],
            "connection_id": connection["connection_id"],
            "purpose": "oauth_bootstrap",
            "expected_generation": 4,
            "bundle": {
                "provider_id": "anthropic",
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
            },
        }
    ]


@pytest.mark.parametrize(
    ("provider_id", "bundle", "expected_key", "expected_mode"),
    [
        (
            "nous",
            {
                "provider_id": "nous",
                "access_token": "portal-token",
                "agent_key": "inference-agent-key",
                "inference_base_url": "https://inference.example/v1",
            },
            "inference-agent-key",
            "chat_completions",
        ),
        (
            "qwen-oauth",
            {
                "provider_id": "qwen-oauth",
                "access_token": "qwen-access",
            },
            "qwen-access",
            "chat_completions",
        ),
        (
            "minimax-oauth",
            {
                "provider_id": "minimax-oauth",
                "access_token": "minimax-access",
                "inference_base_url": "https://api.minimax.io/anthropic",
            },
            "minimax-access",
            "anthropic_messages",
        ),
    ],
)
def test_managed_runtime_uses_broker_bundle_without_auth_file_fallback(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    bundle: dict,
    expected_key: str,
    expected_mode: str,
):
    connection = _install_connection(monkeypatch, provider_id)
    connection["enabled"] = True

    class Resolver:
        def resolve(self, credential_ref: str, **kwargs):
            assert credential_ref == connection["credential_ref"]
            assert kwargs["connection_id"] == connection["connection_id"]
            return ResolvedCredential(
                value=json.dumps(bundle),
                generation=9,
                lease_id="lease-9",
                secret_kind="oauth_bundle",
            )

        def report_result(self, *args, **kwargs):
            return None

    from hermes_cli import credential_resolver, runtime_provider

    monkeypatch.setattr(credential_resolver, "runtime_credential_resolver", Resolver)

    runtime = runtime_provider.resolve_runtime_provider(
        requested=provider_id,
        connection_id=connection["connection_id"],
        target_model="test-model",
    )

    assert runtime["api_key"] == expected_key
    assert runtime["api_mode"] == expected_mode
    assert runtime["credential_generation"] == 9
    assert runtime["credential_secret_kind"] == "oauth_bundle"


def test_anthropic_completion_after_cancel_never_calls_credential_sink(
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_ANTHROPIC_OAUTH_AVAILABLE", True)
    sink_calls = []
    started = web_server._start_anthropic_pkce(sink_calls.append)
    session_id = started["session_id"]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            managed_oauth.cancel_managed_oauth(
                provider_id="anthropic",
                session_id=session_id,
            )
            return json.dumps(
                {
                    "access_token": "must-not-be-stored",
                    "refresh_token": "must-not-be-stored",
                    "expires_in": 3600,
                }
            ).encode()

    monkeypatch.setattr(web_server.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    result = web_server._submit_anthropic_pkce(session_id, "authorization-code")

    assert result["status"] == "cancelled"
    assert sink_calls == []
