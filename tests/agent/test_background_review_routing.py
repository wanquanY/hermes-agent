from __future__ import annotations

from types import SimpleNamespace

from agent.background_review import _digest_history, _resolve_review_runtime


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        provider="openai",
        model="gpt-main",
        max_tokens=8192,
        acp_command=None,
        acp_args=[],
        request_overrides={"service_tier": "auto"},
        _credential_pool="pool",
        _current_main_runtime=lambda: {
            "api_mode": "responses",
            "api_key": "main-key",
            "base_url": "https://main.example/v1",
        },
    )


def test_background_review_auto_reuses_parent_runtime(monkeypatch):
    from hermes_cli import config

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"auxiliary": {"background_review": {"provider": "auto"}}},
    )

    runtime = _resolve_review_runtime(_agent())

    assert runtime["routed"] is False
    assert runtime["model"] == "gpt-main"
    assert runtime["credential_pool"] == "pool"


def test_background_review_explicit_model_uses_isolated_runtime(monkeypatch):
    from hermes_cli import config, runtime_provider

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "auxiliary": {
                "background_review": {
                    "provider": "openrouter",
                    "model": "vendor/reviewer",
                    "extra_body": {"temperature": 0.1},
                }
            }
        },
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://review.example/v1",
            "api_key": "review-key",
            "request_overrides": {"extra_body": {"top_p": 0.8}},
        },
    )

    runtime = _resolve_review_runtime(_agent())

    assert runtime["routed"] is True
    assert runtime["model"] == "vendor/reviewer"
    assert runtime["api_key"] == "review-key"
    assert runtime["request_overrides"] == {
        "extra_body": {"top_p": 0.8, "temperature": 0.1}
    }


def test_background_review_resolution_failure_falls_back(monkeypatch):
    from hermes_cli import config, runtime_provider

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "auxiliary": {
                "background_review": {
                    "provider": "missing",
                    "model": "reviewer",
                }
            }
        },
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: None,
    )

    runtime = _resolve_review_runtime(_agent())

    assert runtime["routed"] is False
    assert runtime["model"] == "gpt-main"


def test_routed_history_digest_preserves_recent_tool_call_boundary():
    messages = [
        {"role": "user", "content": f"old question {index}"}
        for index in range(10)
    ]
    messages.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "memory", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "content": "saved", "tool_call_id": "call-1"},
            {"role": "assistant", "content": "done"},
        ]
    )

    digested = _digest_history(messages, tail=2)

    assert digested[0]["role"] == "user"
    assert "Earlier conversation digest" in digested[0]["content"]
    assert digested[1]["role"] == "assistant"
    assert digested[2]["role"] == "tool"
    assert digested[-1]["content"] == "done"
