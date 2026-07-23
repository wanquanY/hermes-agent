from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent_capabilities.cloud_gateway import call_task_hub_gateway
from agent_capabilities.credentials import CapabilityCredentialRegistry


def _configure(registry: CapabilityCredentialRegistry, **overrides):
    values = {
        "capability": "dovie.task_hub_assistant@1",
        "token": "secret-capability-token",
        "api_origin": "https://api.doviemate.com",
        "conversation_id": "conversation-1",
        "execution_participant_id": "default-assistant-1",
        "expires_at": time.time() + 600,
    }
    values.update(overrides)
    return registry.configure(**values)


def test_capability_credentials_are_conversation_bound_and_clearable():
    registry = CapabilityCredentialRegistry()
    configured = _configure(registry)

    resolved = registry.resolve(
        capability="dovie.task_hub_assistant@1",
        conversation_id="conversation-1",
    )

    assert resolved == configured
    assert resolved.fingerprint
    with pytest.raises(RuntimeError, match="not configured"):
        registry.resolve(
            capability="dovie.task_hub_assistant@1",
            conversation_id="conversation-2",
        )
    assert registry.clear(
        capability="dovie.task_hub_assistant@1",
        conversation_id="conversation-1",
    ) is True


def test_capability_credentials_reject_unsafe_origin_and_expired_token():
    registry = CapabilityCredentialRegistry()

    with pytest.raises(ValueError, match="origin"):
        _configure(registry, api_origin="file:///tmp/dovie")
    with pytest.raises(ValueError, match="expired"):
        _configure(registry, expires_at=time.time() - 1)


def test_capability_configuration_rolls_back_when_tool_surface_refresh_fails():
    registry = CapabilityCredentialRegistry()

    with pytest.raises(RuntimeError, match="refresh failed"):
        _configure(
            registry,
            on_change=lambda: (_ for _ in ()).throw(RuntimeError("refresh failed")),
        )

    with pytest.raises(RuntimeError, match="not configured"):
        registry.resolve(
            capability="dovie.task_hub_assistant@1",
            conversation_id="conversation-1",
        )


def test_capability_clear_rolls_back_when_tool_surface_refresh_fails():
    registry = CapabilityCredentialRegistry()
    configured = _configure(registry)

    with pytest.raises(RuntimeError, match="refresh failed"):
        registry.clear(
            capability=configured.capability,
            conversation_id=configured.conversation_id,
            on_change=lambda: (_ for _ in ()).throw(RuntimeError("refresh failed")),
        )

    assert registry.resolve(
        capability=configured.capability,
        conversation_id=configured.conversation_id,
    ) == configured


def test_cloud_transport_uses_allowlisted_path_and_injects_runtime_context(monkeypatch):
    credential = _configure(CapabilityCredentialRegistry())
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"schema_version":1,"data":{"items":[]}}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = call_task_hub_gateway(
        tool_name="task_hub_search",
        arguments={"keyword": "Python"},
        tool_call_id="tool-1",
        credential=credential,
    )

    assert result["schema_version"] == 1
    assert captured["url"].endswith("/api/v1/agent-capabilities/task-hub/v1/search")
    assert captured["authorization"] == "Bearer secret-capability-token"
    assert captured["body"] == {
        "keyword": "Python",
        "tool_call_id": "tool-1",
        "conversation_id": "conversation-1",
        "execution_participant_id": "default-assistant-1",
    }


def test_tool_handler_uses_parent_agent_session_not_model_arguments(monkeypatch):
    from tools import dovie_task_hub_tools

    calls = []
    credential = SimpleNamespace(
        conversation_id="conversation-1",
        execution_participant_id="default-assistant-1",
    )
    monkeypatch.setattr(
        dovie_task_hub_tools.capability_credentials,
        "resolve",
        lambda **kwargs: credential,
    )
    monkeypatch.setattr(
        dovie_task_hub_tools,
        "call_task_hub_gateway",
        lambda **kwargs: calls.append(kwargs) or {"schema_version": 1, "data": {}},
    )

    result = json.loads(
        dovie_task_hub_tools._invoke(
            "task_hub_search",
            {"keyword": "Python"},
            parent_agent=SimpleNamespace(session_id="conversation-1"),
            tool_call_id="tool-1",
        )
    )

    assert result["schema_version"] == 1
    assert calls[0]["arguments"] == {"keyword": "Python"}
    assert calls[0]["credential"] is credential
