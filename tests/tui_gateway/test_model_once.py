from __future__ import annotations

from types import SimpleNamespace


class _Agent:
    def __init__(self):
        self.model = "old/model"
        self.provider = "openrouter"
        self.api_key = "sk-old"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_mode = "chat_completions"
        self.calls = []

    def switch_model(self, **kwargs):
        self.calls.append(kwargs)
        self.model = kwargs["new_model"]
        self.provider = kwargs["new_provider"]
        self.api_key = kwargs["api_key"]
        self.base_url = kwargs["base_url"]
        self.api_mode = kwargs["api_mode"]


def _result(model: str, provider: str = "anthropic") -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        new_model=model,
        target_provider=provider,
        api_key=f"key-{model}",
        base_url=f"https://{provider}.example/v1",
        api_mode="anthropic_messages",
        warning_message="",
        provider_label=provider,
        model_info=None,
    )


def _patch_switch_runtime(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"persist_switch_by_default": True}},
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_compatible_custom_providers",
        lambda config: {},
    )
    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.expensive_model_warning",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *args: None)
    monkeypatch.setattr(server, "_persist_live_session_runtime", lambda *args: None)
    monkeypatch.setattr(
        server,
        "_persist_live_session_system_prompt",
        lambda *args: None,
    )
    monkeypatch.setattr(server, "_append_model_switch_marker", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda *args, **kwargs: {})
    return server


def test_tui_once_does_not_replace_session_override(monkeypatch):
    server = _patch_switch_runtime(monkeypatch)
    agent = _Agent()
    original_override = {
        "model": "old/model",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
    }
    session = {
        "agent": agent,
        "history": [],
        "model_override": dict(original_override),
    }
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kwargs: _result("temporary/a"),
    )

    result = server._apply_model_switch(
        "sid",
        session,
        "temporary/a --provider anthropic --once",
    )

    assert result["scope"] == "once"
    assert agent.model == "temporary/a"
    assert session["model_override"] == original_override
    restore = session[server._ONE_TURN_MODEL_RESTORE_KEY]
    assert restore["agent_runtime"]["model"] == "old/model"
    assert restore["model_override"] == original_override


def test_tui_repeated_once_restores_original_baseline(monkeypatch):
    server = _patch_switch_runtime(monkeypatch)
    agent = _Agent()
    session = {"agent": agent, "history": []}
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda raw_input, **kwargs: _result(raw_input),
    )

    server._apply_model_switch("sid", session, "temporary/a --once")
    first_restore = session[server._ONE_TURN_MODEL_RESTORE_KEY]
    server._apply_model_switch("sid", session, "temporary/b --once")

    assert session[server._ONE_TURN_MODEL_RESTORE_KEY] is first_restore
    assert first_restore["agent_runtime"]["model"] == "old/model"
    assert agent.model == "temporary/b"

    consumed = session.pop(server._ONE_TURN_MODEL_RESTORE_KEY)
    server._restore_session_model_runtime(session, consumed)

    assert agent.model == "old/model"
    assert agent.provider == "openrouter"
    assert "model_override" not in session


def test_tui_session_switch_cancels_pending_once_restore(monkeypatch):
    server = _patch_switch_runtime(monkeypatch)
    agent = _Agent()
    session = {"agent": agent, "history": []}
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda raw_input, **kwargs: _result(raw_input),
    )

    server._apply_model_switch("sid", session, "temporary/a --once")
    result = server._apply_model_switch(
        "sid",
        session,
        "permanent/b --session",
    )

    assert result["scope"] == "session"
    assert server._ONE_TURN_MODEL_RESTORE_KEY not in session
    assert session["model_override"]["model"] == "permanent/b"


def test_tui_once_requires_live_agent(monkeypatch):
    server = _patch_switch_runtime(monkeypatch)

    try:
        server._apply_model_switch(
            "sid",
            {"agent": None},
            "temporary/a --provider anthropic --once",
        )
    except ValueError as exc:
        assert str(exc) == "/model --once requires a live session"
    else:
        raise AssertionError("one-turn switch unexpectedly accepted without agent")
