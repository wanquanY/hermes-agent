from types import SimpleNamespace

from agent.provider_telemetry import (
    PROVIDER_TELEMETRY_EVENT_TYPE,
    PROVIDER_TELEMETRY_SCHEMA,
    ProviderCallTelemetry,
    emit_provider_telemetry,
)


def _agent():
    context = SimpleNamespace(
        conversation_session_id="conversation-1",
        execution_scope_key="profile:agent-1",
        activity_id="activity-1",
        participant_id="participant-1",
    )
    return SimpleNamespace(
        session_id="execution-1",
        provider="openrouter",
        model="openai/gpt-5",
        api_mode="chat_completions",
        _hermes_active_run_id="run-1",
        _hermes_active_turn_id="turn-1",
        _hermes_active_runtime_scope_key="profile:agent-1",
        _current_api_request_id="turn-1:api:1",
        _active_run_context=lambda: context,
    )


def test_emit_provider_telemetry_is_content_free_and_transient(monkeypatch):
    published = []

    def _publish(params, **kwargs):
        published.append((params, kwargs))
        return []

    monkeypatch.setattr(
        "tui_gateway.services.run_control.publish_recorded_event",
        _publish,
    )
    error = TimeoutError(
        "secret prompt body https://provider.invalid Authorization=Bearer-secret"
    )

    emit_provider_telemetry(
        _agent(),
        "provider.call.failed",
        provider_call_id="turn-1:api:1:logical:1",
        labels={
            "reason_code": "timeout",
            "unsafe": "prompt text with whitespace",
        },
        metrics={"duration_ms": 123.4567},
        error=error,
    )

    assert len(published) == 1
    params, kwargs = published[0]
    assert kwargs == {"persist": False}
    assert params["type"] == PROVIDER_TELEMETRY_EVENT_TYPE
    assert params["event_domain"] == "diagnostics"
    assert params["transient"] is True
    assert params["conversation_session_id"] == "conversation-1"
    assert params["run_id"] == "run-1"
    assert params["payload"] == {
        "schema": PROVIDER_TELEMETRY_SCHEMA,
        "stage": "provider.call.failed",
        "provider_call_id": "turn-1:api:1:logical:1",
        "labels": {
            "reason_code": "timeout",
            "provider": "openrouter",
            "model": "openai/gpt-5",
            "api_mode": "chat_completions",
            "error_class": "TimeoutError",
        },
        "metrics": {"duration_ms": 123.457},
    }
    serialized = repr(params)
    assert "secret prompt body" not in serialized
    assert "Bearer-secret" not in serialized
    assert "provider.invalid" not in serialized


def test_provider_call_telemetry_emits_each_first_signal_once(monkeypatch):
    events = []
    monkeypatch.setattr(
        "tui_gateway.services.run_control.publish_recorded_event",
        lambda params, **_kwargs: events.append(params) or [],
    )
    agent = _agent()
    context = ProviderCallTelemetry.start(
        agent,
        logical_attempt=2,
        max_logical_attempts=4,
        stream_mode="streaming",
    )

    context.begin_network_attempt(1, 3)
    context.observe_headers(1, SimpleNamespace(status_code=200))
    context.observe_headers(1, SimpleNamespace(status_code=500))
    context.observe_first_event(1)
    context.observe_first_event(1)
    context.observe_first_delta(1)
    context.observe_first_delta(1)
    context.retry_scheduled(
        failed_attempt=1,
        next_attempt=2,
        max_attempts=3,
        error=ConnectionError("must not escape"),
        retry_kind="transport",
    )
    context.completed()

    stages = [event["payload"]["stage"] for event in events]
    assert stages == [
        "provider.call.started",
        "provider.attempt.started",
        "provider.response.headers",
        "provider.response.first_event",
        "provider.response.first_delta",
        "provider.attempt.failed",
        "provider.attempt.retry_scheduled",
        "provider.call.completed",
    ]
    assert events[2]["payload"]["metrics"]["status_code"] == 200
    assert events[2]["payload"]["metrics"]["time_to_headers_ms"] >= 0
    assert events[3]["payload"]["metrics"]["time_to_first_event_ms"] >= 0
    assert events[4]["payload"]["metrics"]["time_to_first_delta_ms"] >= 0
    assert all("must not escape" not in repr(event) for event in events)


def test_provider_telemetry_is_disabled_without_run_or_scope(monkeypatch):
    published = []
    monkeypatch.setattr(
        "tui_gateway.services.run_control.publish_recorded_event",
        lambda params, **kwargs: published.append((params, kwargs)) or [],
    )
    agent = _agent()
    agent._hermes_active_run_id = ""
    agent._hermes_active_runtime_scope_key = ""
    agent._active_run_context = lambda: None

    emit_provider_telemetry(agent, "provider.call.started")

    assert published == []
