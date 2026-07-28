def test_prompt_stage_diagnostic_does_not_use_logging_lock(monkeypatch):
    from tui_gateway.methods import prompt

    called = {"warning": False}

    def fail_warning(*_args, **_kwargs):
        called["warning"] = True
        raise AssertionError("DoXie stage diagnostics must not use logging.warning")

    monkeypatch.setattr(prompt.logger, "warning", fail_warning)

    prompt._log_prompt_stage(
        {
            "active_run_id": "run-1",
            "active_turn_id": "turn-1",
            "runtime_scope_key": "team:conversation:leader-conversation",
            "session_key": "team-session-1",
        },
        "runtime-1",
        "before-agent-run",
    )

    assert called["warning"] is False


def test_stream_stage_diagnostic_does_not_use_logging_lock(monkeypatch):
    from types import SimpleNamespace

    from agent import chat_completion_helpers

    called = {"warning": False}

    def fail_warning(*_args, **_kwargs):
        called["warning"] = True
        raise AssertionError("DoXie stream diagnostics must not use logging.warning")

    monkeypatch.setattr(chat_completion_helpers.logger, "warning", fail_warning)

    agent = SimpleNamespace(
        _hermes_active_run_id="run-1",
        _hermes_active_turn_id="turn-1",
        _hermes_active_runtime_scope_key="team:conversation:leader-conversation",
        session_id="team-session-1",
    )

    chat_completion_helpers._log_dovie_stream_stage(
        agent,
        "chat-completions-create-start",
        model="deepseek-v4-pro",
    )

    assert called["warning"] is False
