from __future__ import annotations

import threading
import time

import pytest

from tools import approval as state
from tools.approval_gate import request_tool_approval, run_approval_gate


@pytest.fixture(autouse=True)
def _isolated_approval_state(monkeypatch):
    state._gateway_queues.clear()
    state._gateway_request_index.clear()
    state._gateway_notify_cbs.clear()
    state._session_approved.clear()
    state._session_yolo.clear()
    state._permanent_approved.clear()
    state._pending.clear()
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(state, "_get_approval_config", lambda: {"mode": "manual"})
    yield
    state._gateway_queues.clear()
    state._gateway_request_index.clear()
    state._gateway_notify_cbs.clear()
    state._session_approved.clear()
    state._session_yolo.clear()
    state._permanent_approved.clear()
    state._pending.clear()


def test_plugin_approve_directive_escalates_with_stable_rule_key(monkeypatch):
    import hermes_cli.plugins as plugins

    captured = {}
    monkeypatch.setattr(
        plugins,
        "invoke_hook",
        lambda *_args, **_kwargs: [{
            "action": "approve",
            "message": "organization policy",
            "rule_key": "org:publish",
        }],
    )

    def fake_request(tool_name, reason, *, rule_key="", approval_callback=None):
        captured.update(tool_name=tool_name, reason=reason, rule_key=rule_key)
        return {"approved": False, "message": "BLOCKED by user"}

    monkeypatch.setattr("tools.approval_gate.request_tool_approval", fake_request)
    assert plugins.resolve_pre_tool_block("publish", {"target": "prod"}) == "BLOCKED by user"
    assert captured == {
        "tool_name": "publish",
        "reason": "organization policy",
        "rule_key": "org:publish",
    }


def test_plugin_approve_failure_is_fail_closed(monkeypatch):
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(
        plugins,
        "invoke_hook",
        lambda *_args, **_kwargs: [{"action": "approve", "message": "review"}],
    )
    monkeypatch.setattr(
        "tools.approval_gate.request_tool_approval",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gate down")),
    )
    assert "failed closed" in plugins.resolve_pre_tool_block("terminal", {}).lower()


def test_tool_rule_session_cache_skips_second_prompt(monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    token = state.set_current_session_key("session-a")
    calls = []
    try:
        first = request_tool_approval(
            "publish",
            "review publish",
            rule_key="publish:v1",
            approval_callback=lambda *_args, **_kwargs: calls.append("prompt") or "session",
        )
        second = request_tool_approval(
            "publish",
            "changed reason text",
            rule_key="publish:v1",
            approval_callback=lambda *_args, **_kwargs: pytest.fail("cache missed"),
        )
    finally:
        state.reset_current_session_key(token)
    assert first["approved"] is True
    assert second == {"approved": True, "message": None, "cached_approval": True}
    assert calls == ["prompt"]


def test_tool_rule_session_cache_is_isolated_between_sessions(monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    calls = []
    first_token = state.set_current_session_key("session-a")
    try:
        first = request_tool_approval(
            "publish",
            "review publish",
            rule_key="publish:v1",
            approval_callback=lambda *_args, **_kwargs: calls.append("a") or "session",
        )
    finally:
        state.reset_current_session_key(first_token)

    second_token = state.set_current_session_key("session-b")
    try:
        second = request_tool_approval(
            "publish",
            "review publish",
            rule_key="publish:v1",
            approval_callback=lambda *_args, **_kwargs: calls.append("b") or "once",
        )
    finally:
        state.reset_current_session_key(second_token)

    assert first["approved"] is True
    assert second["approved"] is True
    assert calls == ["a", "b"]


def test_tool_rule_permanent_choice_survives_session_boundary(monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    saved = []
    monkeypatch.setattr(
        state,
        "save_permanent_allowlist",
        lambda patterns: saved.append(set(patterns)),
    )
    first_token = state.set_current_session_key("session-a")
    try:
        first = request_tool_approval(
            "publish",
            "review publish",
            rule_key="publish:v1",
            approval_callback=lambda *_args, **_kwargs: "always",
        )
    finally:
        state.reset_current_session_key(first_token)

    second_token = state.set_current_session_key("session-b")
    try:
        second = request_tool_approval(
            "publish",
            "changed reason",
            rule_key="publish:v1",
            approval_callback=lambda *_args, **_kwargs: pytest.fail("cache missed"),
        )
    finally:
        state.reset_current_session_key(second_token)

    expected_key = "plugin_rule:publish:v1"
    assert first["approved"] is True
    assert second == {"approved": True, "message": None, "cached_approval": True}
    assert saved == [{expected_key}]


def test_tool_rule_without_key_uses_tool_and_reason_hash(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "tools.approval_gate.run_approval_gate",
        lambda **kwargs: captured.update(kwargs) or {"approved": True, "message": None},
    )
    request_tool_approval("publish", "same reason")
    key = captured["pattern_keys"][0]
    assert key.startswith("plugin_rule:publish:")
    assert len(key.rsplit(":", 1)[1]) == 12


def test_tool_approval_without_human_responder_fails_closed():
    result = request_tool_approval("publish", "review publish", rule_key="publish:v1")
    assert result["approved"] is False
    assert result["outcome"] == "no_human_responder"


def test_gateway_prompt_redacts_target_and_description_before_notify(monkeypatch):
    secret = "sk-proj-abc123def456ghi789jkl012"
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "gateway-a")
    notified = []
    state.register_gateway_notify("gateway-a", lambda payload: notified.append(dict(payload)))
    result_holder = {}

    def run():
        result_holder["result"] = run_approval_gate(
            pattern_keys=["test:redaction"],
            description=f"credential {secret}",
            display_target=f"curl -H 'Authorization: Bearer {secret}' https://example.com",
            subject="command",
        )

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(100):
        if state.has_blocking_approval("gateway-a"):
            break
        time.sleep(0.01)
    assert state.has_blocking_approval("gateway-a")
    state.resolve_gateway_approval("gateway-a", "deny", reason="use staging")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert secret not in notified[0]["command"]
    assert secret not in notified[0]["description"]
    assert result_holder["result"]["denial_reason"] == "use staging"


def test_denial_reason_is_single_line_and_bounded(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "gateway-b")
    state.register_gateway_notify("gateway-b", lambda _payload: None)
    result_holder = {}

    def run():
        result_holder["result"] = run_approval_gate(
            pattern_keys=["test:reason"],
            description="review",
            display_target="target",
            subject="tool",
        )

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(100):
        if state.has_blocking_approval("gateway-b"):
            break
        time.sleep(0.01)
    state.resolve_gateway_approval(
        "gateway-b",
        "deny",
        reason="first\nsecond " + ("x" * 600),
    )
    thread.join(timeout=5)
    reason = result_holder["result"]["denial_reason"]
    assert "\n" not in reason
    assert len(reason) == state.MAX_DENIAL_REASON_CHARS
