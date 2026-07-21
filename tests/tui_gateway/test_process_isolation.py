from __future__ import annotations

import pytest

from hermes_agent.orchestration import worker_runtime
from tui_gateway.services.process_isolation import load_dashboard_process_isolation


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), ("yes", True), ("0", False), (None, False)],
)
def test_process_isolation_policy_normalizes_dashboard_config(value, expected):
    policy = load_dashboard_process_isolation(
        {"dashboard": {"turn_isolation": value}}
    )
    assert policy.turn_isolation is expected


@pytest.mark.asyncio
async def test_default_dashboard_turn_uses_canonical_worker_when_enabled(
    monkeypatch,
):
    captured = {}

    async def dispatch(req, transport, scope, params):
        captured.update(
            {
                "req": req,
                "transport": transport,
                "scope": scope,
                "params": params,
            }
        )
        return True

    monkeypatch.setattr(
        "tui_gateway.services.process_isolation.load_dashboard_process_isolation",
        lambda: load_dashboard_process_isolation(
            {"dashboard": {"turn_isolation": True}}
        ),
    )
    monkeypatch.setattr(worker_runtime, "_dispatch_prompt_submit", dispatch)
    request = {
        "id": "turn",
        "method": "prompt.submit",
        "params": {"session_id": "session-1", "text": "hello"},
    }
    transport = object()

    assert await worker_runtime.primary_dispatch(request, transport) is True
    assert captured["scope"].agent_profile_id == "agent-default"
    assert captured["scope"].runtime_scope_key == "profile:agent-default"
    assert captured["params"]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_default_dashboard_turn_stays_inline_when_isolation_disabled(
    monkeypatch,
):
    monkeypatch.setattr(
        "tui_gateway.services.process_isolation.load_dashboard_process_isolation",
        lambda: load_dashboard_process_isolation(
            {"dashboard": {"turn_isolation": False}}
        ),
    )
    request = {
        "id": "turn",
        "method": "prompt.submit",
        "params": {"session_id": "session-1", "text": "hello"},
    }
    assert await worker_runtime.primary_dispatch(request, object()) is False
