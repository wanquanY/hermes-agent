from types import SimpleNamespace

from agent.codex_responses_adapter import _preflight_codex_api_kwargs
from agent.transports.codex import ResponsesApiTransport
from tui_gateway.services.model_descriptor import set_session_model_descriptor


def test_responses_descriptor_emits_only_canonical_reasoning_field_each_turn():
    agent = SimpleNamespace(
        api_mode="chat_completions",
        reasoning_config=None,
        request_overrides={
            "reasoning_effort": "low",
            "service_tier": "default",
        },
        _transport_cache={},
        _primary_runtime={"api_mode": "chat_completions"},
    )
    session = {"agent": agent}
    descriptor = {
        "id": "deepseek-v4-pro",
        "api_format": "openai_responses",
        "reasoning_enabled": True,
        "reasoning_efforts": ["low", "medium", "high"],
        "reasoning_effort": "high",
        "request_params": {
            "reasoning_effort": "medium",
            "service_tier": "priority",
        },
    }

    # Dovie reapplies the authoritative descriptor on every submitted turn.
    # Both the first request and every continuation must keep the same native
    # Responses shape without accumulating a Chat Completions alias.
    for _ in range(2):
        set_session_model_descriptor(session, descriptor)
        api_kwargs = ResponsesApiTransport().build_kwargs(
            model="deepseek-v4-pro",
            messages=[
                {"role": "system", "content": "You are Dovie."},
                {"role": "user", "content": "What time is it?"},
            ],
            reasoning_config=agent.reasoning_config,
            request_overrides=agent.request_overrides,
        )

        assert api_kwargs["reasoning"] == {"effort": "high", "summary": "auto"}
        assert "reasoning_effort" not in api_kwargs
        assert _preflight_codex_api_kwargs(api_kwargs)["reasoning"] == {
            "effort": "high",
            "summary": "auto",
        }

    assert agent.request_overrides == {"service_tier": "priority"}

    set_session_model_descriptor(session, {}, clear_if_empty=True)

    assert agent.request_overrides == {
        "reasoning_effort": "low",
        "service_tier": "default",
    }


def test_managed_connection_descriptor_cannot_replace_route_protocol():
    agent = SimpleNamespace(
        api_mode="anthropic_messages",
        reasoning_config=None,
        request_overrides={},
        _transport_cache={},
        _primary_runtime={"api_mode": "anthropic_messages"},
        _managed_connection_id="builtin:kimi-coding",
    )
    session = {"agent": agent}

    set_session_model_descriptor(
        session,
        {
            "id": "kimi-k3",
            "api_format": "openai",
            "reasoning_enabled": True,
            "reasoning_efforts": ["low", "medium", "high"],
        },
    )

    assert agent.api_mode == "anthropic_messages"
    assert agent.model_descriptor["id"] == "kimi-k3"
