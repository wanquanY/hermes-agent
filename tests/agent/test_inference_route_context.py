from __future__ import annotations

import pytest

from agent import auxiliary_client
from agent.inference_route_context import (
    direct_only_route_active,
    push_inference_route,
    reset_inference_route,
)


def _direct_resolution() -> dict:
    return {
        "route": {
            "route_kind": "hermes_direct",
            "cloud_usage_policy": "direct_only",
        }
    }


def test_direct_route_closes_auxiliary_auto_fallback(monkeypatch):
    fallback_called = False

    def unavailable(*_args, **_kwargs):
        return None, None

    def fallback_chain():
        nonlocal fallback_called
        fallback_called = True
        return []

    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", unavailable)
    monkeypatch.setattr(auxiliary_client, "_get_provider_chain", fallback_chain)
    tokens = push_inference_route(
        _direct_resolution(),
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "test-secret",
            "api_mode": "chat_completions",
        },
    )
    try:
        client, model = auxiliary_client._resolve_auto()
        assert client is None
        assert model is None
        assert fallback_called is False
        assert direct_only_route_active() is True
    finally:
        reset_inference_route(tokens)
    assert direct_only_route_active() is False


def test_direct_route_does_not_payment_fallback(monkeypatch):
    class PaymentFailure(Exception):
        pass

    class Completions:
        @staticmethod
        def create(**_kwargs):
            raise PaymentFailure("payment required")

    class Client:
        base_url = "https://provider.example/v1"
        chat = type("Chat", (), {"completions": Completions()})()

    fallback_called = False

    def fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        return None, None, ""

    monkeypatch.setattr(
        auxiliary_client,
        "_resolve_task_provider_model",
        lambda *_args, **_kwargs: ("openrouter", "other-model", "", "", ""),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_get_cached_client",
        lambda *_args, **_kwargs: (Client(), "deepseek-chat"),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_is_payment_error",
        lambda error: isinstance(error, PaymentFailure),
    )
    monkeypatch.setattr(auxiliary_client, "_try_payment_fallback", fallback)
    tokens = push_inference_route(
        _direct_resolution(),
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://provider.example/v1",
            "api_key": "test-secret",
            "api_mode": "chat_completions",
        },
    )
    try:
        with pytest.raises(PaymentFailure):
            auxiliary_client.call_llm(
                task="compression",
                messages=[{"role": "user", "content": "compress"}],
            )
    finally:
        reset_inference_route(tokens)
    assert fallback_called is False
