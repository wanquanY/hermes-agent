"""Behavioral contract for the single Gateway conversation boundary funnel."""

from __future__ import annotations

from types import SimpleNamespace

from hermes_gateway.session_runtime_state import (
    CONVERSATION_SCOPED_STATE,
    session_runtime_state_for,
)

TARGET = "agent:main:telegram:dm:target"
OTHER = "agent:main:discord:dm:other"


def _runner_with_scope_state():
    runner = SimpleNamespace()
    for attribute in CONVERSATION_SCOPED_STATE:
        setattr(runner, attribute, {TARGET: object(), OTHER: object()})
    runner._pending_approvals = {TARGET: object(), OTHER: object()}
    runner._update_prompt_pending = {TARGET: True, OTHER: True}
    runner._pending_skills_reload_notes = {TARGET: "target", OTHER: "other"}
    runner._running_agents = {TARGET: object()}
    runner._running_agents_ts = {TARGET: 1.0}
    runner._session_run_generation = {TARGET: 7}
    return runner


def test_funnel_clears_every_registered_mapping_for_target_only(monkeypatch):
    runner = _runner_with_scope_state()
    monkeypatch.setattr("tools.slash_confirm.clear", lambda _key: None)
    monkeypatch.setattr("tools.approval.clear_session", lambda _key: None)

    session_runtime_state_for(runner).clear_conversation_scope(
        TARGET,
        reason="test",
    )

    for attribute in CONVERSATION_SCOPED_STATE:
        mapping = getattr(runner, attribute)
        assert TARGET not in mapping, f"{attribute} retained target state"
        assert OTHER in mapping, f"{attribute} cleared another conversation"
    assert TARGET not in runner._pending_approvals
    assert TARGET not in runner._update_prompt_pending
    assert TARGET not in runner._pending_skills_reload_notes
    assert OTHER in runner._pending_approvals


def test_funnel_preserves_turn_state_and_monotonic_generation(monkeypatch):
    runner = _runner_with_scope_state()
    monkeypatch.setattr("tools.slash_confirm.clear", lambda _key: None)
    monkeypatch.setattr("tools.approval.clear_session", lambda _key: None)

    session_runtime_state_for(runner).clear_conversation_scope(
        TARGET,
        reason="test",
    )

    assert TARGET in runner._running_agents
    assert TARGET in runner._running_agents_ts
    assert runner._session_run_generation[TARGET] == 7


def test_funnel_is_bare_runner_safe_and_empty_key_is_noop(monkeypatch):
    runner = SimpleNamespace()
    monkeypatch.setattr("tools.slash_confirm.clear", lambda _key: None)
    monkeypatch.setattr("tools.approval.clear_session", lambda _key: None)
    service = session_runtime_state_for(runner)

    service.clear_conversation_scope(TARGET, reason="test")
    service.clear_conversation_scope("", reason="test")
