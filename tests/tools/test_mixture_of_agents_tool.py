import importlib
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.usage_pricing import CostResult

moa = importlib.import_module("tools.mixture_of_agents_tool")


def test_moa_defaults_are_well_formed():
    # Invariants, not a catalog snapshot: the exact model list churns with
    # OpenRouter availability (see PR #6636 where gemini-3-pro-preview was
    # removed upstream). What we care about is that the defaults are present
    # and valid vendor/model slugs.
    assert isinstance(moa.REFERENCE_MODELS, list)
    assert len(moa.REFERENCE_MODELS) >= 1
    for m in moa.REFERENCE_MODELS:
        assert isinstance(m, str) and "/" in m and not m.startswith("/")
    assert isinstance(moa.AGGREGATOR_MODEL, str)
    assert "/" in moa.AGGREGATOR_MODEL


@pytest.mark.asyncio
async def test_reference_model_retry_warnings_avoid_exc_info_until_terminal_failure(monkeypatch):
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=RuntimeError("rate limited"))
            )
        )
    )
    warn = MagicMock()
    err = MagicMock()

    monkeypatch.setattr(moa, "_get_openrouter_client", lambda: fake_client)
    monkeypatch.setattr(moa.logger, "warning", warn)
    monkeypatch.setattr(moa.logger, "error", err)

    model, message, success = await moa._run_reference_model_safe(
        "openai/gpt-5.4-pro", "hello", max_retries=2
    )

    assert model == "openai/gpt-5.4-pro"
    assert success is False
    assert "failed after 2 attempts" in message
    assert warn.call_count == 2
    assert all(call.kwargs.get("exc_info") is None for call in warn.call_args_list)
    err.assert_called_once()
    assert err.call_args.kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_moa_top_level_error_logs_single_traceback_on_aggregator_failure(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        moa,
        "_run_reference_model_safe",
        AsyncMock(return_value=("anthropic/claude-opus-4.6", "ok", True)),
    )
    monkeypatch.setattr(
        moa,
        "_run_aggregator_model",
        AsyncMock(side_effect=RuntimeError("aggregator boom")),
    )
    monkeypatch.setattr(
        moa,
        "_debug",
        SimpleNamespace(log_call=MagicMock(), save=MagicMock(), active=False),
    )

    err = MagicMock()
    monkeypatch.setattr(moa.logger, "error", err)

    result = json.loads(
        await moa.mixture_of_agents_tool(
            "solve this",
            reference_models=["anthropic/claude-opus-4.6"],
        )
    )

    assert result["success"] is False
    assert "Error in MoA processing" in result["error"]
    err.assert_called_once()
    assert err.call_args.kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_moa_records_four_references_and_aggregator_with_real_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    models = [f"vendor/reference-{index}" for index in range(4)]
    aggregator = "vendor/aggregator"
    responses = [
        SimpleNamespace(
            content=f"response-{index}",
            usage=SimpleNamespace(
                prompt_tokens=10 + index,
                completion_tokens=5,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        )
        for index in range(5)
    ]
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
        )
    )

    class UsageDB:
        def __init__(self):
            self.sessions = self
            self.calls = []

        def update_token_counts(self, session_id, **kwargs):
            self.calls.append({"session_id": session_id, **kwargs})

    parent_agent = SimpleNamespace(
        api_key="",
        session_id="session-moa",
        _session_db=UsageDB(),
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

    monkeypatch.setattr(moa, "_get_openrouter_client", lambda: client)
    monkeypatch.setattr(moa, "extract_content_or_reasoning", lambda response: response.content)
    monkeypatch.setattr(
        "agent.model_usage_recorder.estimate_usage_cost",
        lambda *_args, **_kwargs: CostResult(
            amount_usd=Decimal("0.01"),
            status="estimated",
            source="official_docs_snapshot",
            label="estimated",
        ),
    )
    monkeypatch.setattr(
        moa,
        "_debug",
        SimpleNamespace(log_call=MagicMock(), save=MagicMock(), active=False),
    )

    result = json.loads(
        await moa.mixture_of_agents_tool(
            "solve this",
            reference_models=models,
            aggregator_model=aggregator,
            parent_agent=parent_agent,
        )
    )

    assert result["success"] is True
    assert client.chat.completions.create.await_count == 5
    records = list(parent_agent._model_usage_records)
    assert [record.attribution.purpose for record in records] == [
        "moa.reference",
        "moa.reference",
        "moa.reference",
        "moa.reference",
        "moa.aggregator",
    ]
    assert [record.attribution.model for record in records] == [*models, aggregator]
    assert parent_agent.session_api_calls == 5
    assert len(parent_agent._session_db.calls) == 5
    assert all("model" not in call for call in parent_agent._session_db.calls)
