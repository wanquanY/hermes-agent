from types import SimpleNamespace

from agent.aux_accounting import (
    record_aux_usage,
    reset_accounting_context,
    set_accounting_context,
)
from agent.auxiliary_client import _validate_llm_response


def _agent():
    return SimpleNamespace(
        api_key="",
        session_id="session-1",
        _session_db=None,
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


def _response():
    return SimpleNamespace(
        model="vendor/aux",
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
    )


def test_validation_records_aux_usage_with_purpose(monkeypatch):
    monkeypatch.setattr("agent.model_usage_recorder.estimate_usage_cost", lambda *_a, **_kw: None)
    agent = _agent()
    token = set_accounting_context(agent=agent)
    try:
        assert _validate_llm_response(_response(), "vision") is not None
    finally:
        reset_accounting_context(token)

    record = list(agent._model_usage_records)[0]
    assert record.attribution.purpose == "aux.vision"
    assert record.attribution.model == "vendor/aux"
    assert agent.session_api_calls == 1
    assert agent.session_total_tokens == 15


def test_moa_tasks_are_not_double_counted(monkeypatch):
    monkeypatch.setattr("agent.model_usage_recorder.estimate_usage_cost", lambda *_a, **_kw: None)
    agent = _agent()
    token = set_accounting_context(agent=agent)
    try:
        record_aux_usage(_response(), "moa_reference")
        record_aux_usage(_response(), "moa_aggregator")
    finally:
        reset_accounting_context(token)

    assert agent.session_api_calls == 0


def test_context_isolation_restores_previous_agent(monkeypatch):
    monkeypatch.setattr("agent.model_usage_recorder.estimate_usage_cost", lambda *_a, **_kw: None)
    outer = _agent()
    inner = _agent()
    outer_token = set_accounting_context(agent=outer)
    try:
        inner_token = set_accounting_context(agent=inner)
        try:
            record_aux_usage(_response(), "compression")
        finally:
            reset_accounting_context(inner_token)
        record_aux_usage(_response(), "web_extract")
    finally:
        reset_accounting_context(outer_token)

    assert [r.attribution.purpose for r in inner._model_usage_records] == [
        "aux.compression"
    ]
    assert [r.attribution.purpose for r in outer._model_usage_records] == [
        "aux.web_extract"
    ]
