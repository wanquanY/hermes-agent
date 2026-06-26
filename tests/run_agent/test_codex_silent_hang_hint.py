"""Tests for the Codex silent-hang hint heuristic."""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_agent(tmp_path: Path, **overrides):
    from run_agent import AIAgent

    kwargs = dict(
        model="gpt-5.5",
        provider="openai-codex",
        api_key="sk-dummy",
        base_url="https://chatgpt.com/backend-api/codex",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


@pytest.fixture(autouse=True)
def _isolate_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")


def test_hint_fires_for_bare_gpt_5_5_on_codex(tmp_path):
    agent = _make_agent(tmp_path)
    agent.api_mode = "codex_responses"
    hint = agent._codex_silent_hang_hint(model="gpt-5.5")
    assert hint is not None
    assert "gpt-5.4" in hint
    assert "gpt-5.3-codex" in hint
    assert "gpt-5.4-codex" in hint
    assert "fallback chain" in hint


def test_hint_fires_for_vendor_prefixed_gpt_5_5(tmp_path):
    agent = _make_agent(tmp_path, model="openai/gpt-5.5")
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint(model="openai/gpt-5.5") is not None


def test_hint_fires_for_gpt_5_5_codex_suffix(tmp_path):
    agent = _make_agent(tmp_path, model="gpt-5.5-codex")
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint(model="gpt-5.5-codex") is not None


def test_hint_fires_when_model_arg_omitted(tmp_path):
    agent = _make_agent(tmp_path)
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint() is not None


def test_hint_skipped_for_gpt_5_4(tmp_path):
    agent = _make_agent(tmp_path, model="gpt-5.4")
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint(model="gpt-5.4") is None


def test_hint_skipped_for_gpt_5_50_false_positive(tmp_path):
    agent = _make_agent(tmp_path, model="gpt-5.50")
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint(model="gpt-5.50") is None


def test_hint_skipped_for_non_codex_api_mode(tmp_path):
    agent = _make_agent(tmp_path)
    agent.api_mode = "chat_completions"
    assert agent._codex_silent_hang_hint(model="gpt-5.5") is None


def test_hint_skipped_for_non_codex_provider(tmp_path):
    agent = _make_agent(
        tmp_path,
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-5.5",
    )
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint(model="openai/gpt-5.5") is None


def test_hint_skipped_for_empty_model(tmp_path):
    agent = _make_agent(tmp_path, model="gpt-5.4")
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint(model="") is None
    assert agent._codex_silent_hang_hint(model=None) is None


def test_hint_skipped_for_unrelated_model_on_codex(tmp_path):
    agent = _make_agent(tmp_path, model="gpt-4-turbo")
    agent.api_mode = "codex_responses"
    assert agent._codex_silent_hang_hint(model="gpt-4-turbo") is None
