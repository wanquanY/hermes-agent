"""Classic CLI conversation boundaries restore durable runtime defaults."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli.cli_session_defaults import (
    capture_initial_model_runtime,
    reset_cli_session_runtime,
)


def _cli_stub() -> SimpleNamespace:
    agent = SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "ultra"},
        service_tier="priority",
        request_overrides={"service_tier": "priority", "keep": "value"},
        switch_model=MagicMock(),
    )
    cli = SimpleNamespace(
        model="configured-model",
        provider="openai",
        requested_provider="openai",
        base_url="https://configured.example/v1",
        api_key="configured-key",
        api_mode="responses",
        _explicit_api_key="configured-key",
        _explicit_base_url="https://configured.example/v1",
        _pending_one_turn_model_restore={"model": "old-model"},
        reasoning_config={"enabled": True, "effort": "ultra"},
        service_tier="priority",
        agent=agent,
    )
    cli._initial_model_runtime = capture_initial_model_runtime(cli)
    return cli


def test_reset_restores_reasoning_fast_and_one_turn_lease() -> None:
    cli = _cli_stub()
    config = {
        "model": {"default": "configured-model", "provider": "openai"},
        "agent": {"reasoning_effort": "low", "service_tier": "normal"},
    }

    reset_cli_session_runtime(cli, config)

    assert cli._pending_one_turn_model_restore is None
    assert cli.reasoning_config == {"enabled": True, "effort": "low"}
    assert cli.service_tier is None
    assert cli.agent.reasoning_config == cli.reasoning_config
    assert cli.agent.service_tier is None
    assert cli.agent.request_overrides == {"keep": "value"}
    cli.agent.switch_model.assert_not_called()


def test_reset_switches_session_model_back_to_configured_route() -> None:
    cli = _cli_stub()
    cli.model = "session-model"
    cli.provider = "anthropic"
    result = SimpleNamespace(
        success=True,
        new_model="configured-model",
        target_provider="openai",
        api_key="configured-key",
        base_url="https://configured.example/v1",
        api_mode="responses",
    )
    config = {
        "model": {"default": "configured-model", "provider": "openai"},
        "agent": {"reasoning_effort": "none", "service_tier": "fast"},
    }

    with (
        patch("hermes_cli.model_switch.switch_model", return_value=result) as switch,
        patch(
            "hermes_cli.models.resolve_fast_mode_overrides",
            return_value={"service_tier": "priority"},
        ),
    ):
        reset_cli_session_runtime(cli, config)

    switch.assert_called_once()
    cli.agent.switch_model.assert_called_once_with(
        new_model="configured-model",
        new_provider="openai",
        api_key="configured-key",
        base_url="https://configured.example/v1",
        api_mode="responses",
    )
    assert cli.model == "configured-model"
    assert cli.provider == "openai"
    assert cli.reasoning_config == {"enabled": False}
    assert cli.service_tier == "priority"
    assert cli.agent.request_overrides == {
        "keep": "value",
        "service_tier": "priority",
    }
