"""Profile credentials must survive API executor and task boundaries."""

from types import SimpleNamespace

import pytest

from agent import secret_scope
from channels.config import PlatformConfig
from channels.platforms.api_server import APIServerAdapter
from channels.platforms.api_server_routing import _api_request_profile
from hermes_gateway.config import GatewayConfig


@pytest.fixture(autouse=True)
def _reset_multiplex():
    secret_scope.set_multiplex_active(False)
    yield
    secret_scope.set_multiplex_active(False)


@pytest.mark.asyncio
async def test_run_agent_reenters_named_profile_inside_executor(
    monkeypatch,
    tmp_path,
):
    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "MODEL_PROVIDER_TOKEN=ops-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profile_home,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: name == "ops",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )

    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"key": "a-strong-api-key-for-tests"},
        )
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True),
    )
    secret_scope.set_multiplex_active(True)

    class _Agent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0
        session_id = "session-1"

        def run_conversation(self, **_kwargs):
            return {
                "final_response": secret_scope.get_secret(
                    "MODEL_PROVIDER_TOKEN"
                )
            }

    monkeypatch.setattr(adapter, "_create_agent", lambda **_kwargs: _Agent())
    token = _api_request_profile.set("ops")
    try:
        result, _usage = await adapter._run_agent(
            "hello",
            [],
            session_id="session-1",
        )
    finally:
        _api_request_profile.reset(token)

    assert result["final_response"] == "ops-secret"


def test_unscoped_reads_still_fail_closed_after_request(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TOKEN=scoped\n", encoding="utf-8")
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"key": "a-strong-api-key-for-tests"},
        )
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: tmp_path,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: True,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    secret_scope.set_multiplex_active(True)

    with adapter._profile_scope(None):
        assert secret_scope.get_secret("TOKEN") == "scoped"

    with pytest.raises(secret_scope.UnscopedSecretError):
        secret_scope.get_secret("TOKEN")
