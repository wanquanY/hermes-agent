"""Application-level model usage recorder tests."""

from decimal import Decimal
from types import SimpleNamespace

from agent.model_usage_recorder import (
    ModelUsageAttribution,
    record_model_response_usage,
)
from agent.usage_pricing import CostResult


class _UsageDB:
    def __init__(self):
        self.sessions = self
        self.calls = []

    def update_token_counts(self, session_id, **kwargs):
        self.calls.append({"session_id": session_id, **kwargs})


def _agent():
    return SimpleNamespace(
        api_key="",
        session_id="session-1",
        _session_db=_UsageDB(),
        _session_db_created=True,
        session_api_calls=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown",
        session_cost_source="none",
    )


def _usage(prompt: int, completion: int):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )


def test_records_each_response_with_real_attribution(monkeypatch):
    monkeypatch.setattr(
        "agent.model_usage_recorder.estimate_usage_cost",
        lambda model, usage, **_kwargs: CostResult(
            amount_usd=Decimal("0.25") if model == "vendor/aux" else Decimal("0.10"),
            status="estimated",
            source="official_docs_snapshot",
            label="estimated",
        ),
    )
    agent = _agent()

    record_model_response_usage(
        agent,
        _usage(10, 4),
        attribution=ModelUsageAttribution(
            purpose="conversation.primary",
            model="vendor/primary",
            provider="openrouter",
            primary=True,
        ),
    )
    record_model_response_usage(
        agent,
        _usage(20, 6),
        attribution=ModelUsageAttribution(
            purpose="moa.reference",
            model="vendor/aux",
            provider="openrouter",
            primary=False,
        ),
    )

    assert agent.session_api_calls == 2
    assert agent.session_input_tokens == 30
    assert agent.session_output_tokens == 10
    assert agent.session_total_tokens == 40
    assert agent.session_estimated_cost_usd == 0.35
    assert [record.attribution.purpose for record in agent._model_usage_records] == [
        "conversation.primary",
        "moa.reference",
    ]
    assert len(agent._session_db.calls) == 2
    assert agent._session_db.calls[0]["model"] == "vendor/primary"
    assert "model" not in agent._session_db.calls[1]
    assert all(call["api_call_count"] == 1 for call in agent._session_db.calls)


def test_response_without_usage_still_counts_api_call(monkeypatch):
    agent = _agent()
    record_model_response_usage(
        agent,
        None,
        attribution=ModelUsageAttribution(
            purpose="codex.primary",
            model="gpt-5.5",
            provider="openai-codex",
            primary=True,
        ),
    )

    assert agent.session_api_calls == 1
    assert agent.session_total_tokens == 0
    assert agent._session_db.calls[0]["api_call_count"] == 1
