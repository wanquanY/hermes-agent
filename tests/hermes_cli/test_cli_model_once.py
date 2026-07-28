from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.model_switch import ModelSwitchResult


class _FakeAgent:
    def __init__(self):
        self.calls = []
        self.model = "old/model"
        self.provider = "openrouter"

    def switch_model(self, **kwargs):
        self.calls.append(kwargs)
        self.model = kwargs["new_model"]
        self.provider = kwargs["new_provider"]


class _StubCLI:
    model = "old/model"
    provider = "openrouter"
    requested_provider = "openrouter"
    api_key = "sk-old"
    _explicit_api_key = "sk-old"
    base_url = "https://openrouter.ai/api/v1"
    _explicit_base_url = "https://openrouter.ai/api/v1"
    api_mode = "chat_completions"
    agent = None
    _pending_model_switch_note = None
    _pending_one_turn_model_restore = None

    def _confirm_expensive_model_switch(self, result):
        return True


def _bind_model_runtime_methods(stub):
    import cli as cli_mod

    stub._snapshot_model_runtime = (
        cli_mod.HermesCLI._snapshot_model_runtime.__get__(stub)
    )
    stub._restore_model_runtime_snapshot = (
        cli_mod.HermesCLI._restore_model_runtime_snapshot.__get__(stub)
    )


def test_cli_model_once_records_original_restore_and_does_not_persist(monkeypatch):
    import cli as cli_mod

    stub = _StubCLI()
    stub.agent = _FakeAgent()
    _bind_model_runtime_methods(stub)
    printed = []
    persisted = []

    monkeypatch.setattr(
        cli_mod,
        "_cprint",
        lambda value, *args, **kwargs: printed.append(str(value)),
    )
    monkeypatch.setattr(
        cli_mod,
        "save_config_value",
        lambda *args, **kwargs: persisted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: SimpleNamespace(
            user_providers=None,
            custom_providers=None,
            with_overrides=lambda **kwargs: SimpleNamespace(
                user_providers=None,
                custom_providers=None,
            ),
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kwargs: ModelSwitchResult(
            success=True,
            new_model="claude-sonnet-4.6",
            target_provider="anthropic",
            api_key="sk-ant",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            provider_label="Anthropic",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *args, **kwargs: None,
    )

    cli_mod.HermesCLI._handle_model_switch(
        stub,
        "/model claude-sonnet-4.6 --provider anthropic --once",
    )

    assert persisted == []
    assert stub.model == "claude-sonnet-4.6"
    assert stub._pending_one_turn_model_restore["model"] == "old/model"
    assert any("next turn only" in line for line in printed)


def test_cli_repeated_once_keeps_first_baseline(monkeypatch):
    import cli as cli_mod

    stub = _StubCLI()
    stub.agent = _FakeAgent()
    _bind_model_runtime_methods(stub)
    first_baseline = stub._snapshot_model_runtime()
    stub._pending_one_turn_model_restore = first_baseline
    stub.model = "temporary/a"
    stub.agent.model = "temporary/a"

    monkeypatch.setattr(cli_mod, "_cprint", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: SimpleNamespace(
            user_providers=None,
            custom_providers=None,
            with_overrides=lambda **kwargs: SimpleNamespace(
                user_providers=None,
                custom_providers=None,
            ),
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kwargs: ModelSwitchResult(
            success=True,
            new_model="temporary/b",
            target_provider="anthropic",
            api_key="sk-b",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *args, **kwargs: None,
    )

    cli_mod.HermesCLI._handle_model_switch(
        stub,
        "/model temporary/b --provider anthropic --once",
    )

    assert stub._pending_one_turn_model_restore is first_baseline
    assert stub._pending_one_turn_model_restore["model"] == "old/model"


def test_cli_restore_model_runtime_snapshot_restores_agent():
    import cli as cli_mod

    stub = _StubCLI()
    stub.agent = _FakeAgent()

    cli_mod.HermesCLI._restore_model_runtime_snapshot(
        stub,
        {
            "model": "old/model",
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "api_key": "sk-old",
            "_explicit_api_key": "sk-old",
            "base_url": "https://openrouter.ai/api/v1",
            "_explicit_base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )

    assert stub.model == "old/model"
    assert stub.provider == "openrouter"
    assert stub.agent.calls[-1]["new_model"] == "old/model"
