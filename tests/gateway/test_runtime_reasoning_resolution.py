from hermes_agent.gateway.runtime_config import load_reasoning_config


def _config():
    return {
        "model": {"default": "gpt-5"},
        "agent": {
            "reasoning_effort": "medium",
            "reasoning_overrides": {
                "gpt-5": "low",
                "claude-opus-4.5": "xhigh",
            },
        },
    }


def test_gateway_default_reasoning_uses_config_model():
    assert load_reasoning_config(_config()) == {"enabled": True, "effort": "low"}


def test_gateway_session_model_reasoning_beats_config_default():
    assert load_reasoning_config(_config(), "anthropic/claude-opus-4-5") == {
        "enabled": True,
        "effort": "xhigh",
    }
