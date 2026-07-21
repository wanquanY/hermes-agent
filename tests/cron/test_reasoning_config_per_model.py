from hermes_constants import resolve_reasoning_config


def test_cron_reasoning_uses_effective_model_override():
    config = {
        "model": {"default": "gpt-5"},
        "agent": {
            "reasoning_effort": "medium",
            "reasoning_overrides": {"anthropic/claude-opus-4.5": "xhigh"},
        },
    }

    assert resolve_reasoning_config(config, "claude-opus-4-5") == {
        "enabled": True,
        "effort": "xhigh",
    }


def test_cron_reasoning_global_yaml_false_stays_disabled():
    config = {
        "model": {"default": "gpt-5"},
        "agent": {
            "reasoning_effort": False,
            "reasoning_overrides": {"claude-opus-4.5": "xhigh"},
        },
    }

    assert resolve_reasoning_config(config, "gpt-5") == {"enabled": False}


def test_cron_reasoning_fallback_model_gets_its_own_override():
    config = {
        "agent": {
            "reasoning_effort": "low",
            "reasoning_overrides": {"kimi-k2.6": "high"},
        }
    }

    assert resolve_reasoning_config(config, "moonshotai/kimi-k2-6") == {
        "enabled": True,
        "effort": "high",
    }
