"""Classic CLI reasoning recap can be switched between full and clamped."""

from types import SimpleNamespace

import cli

from hermes_cli.config import DEFAULT_CONFIG


def _stub():
    return SimpleNamespace(
        reasoning_config=None,
        show_reasoning=True,
        reasoning_full=False,
        agent=None,
    )


def test_reasoning_recap_defaults_to_clamped():
    assert DEFAULT_CONFIG["display"]["reasoning_full"] is False


def test_reasoning_full_sets_and_persists(monkeypatch):
    writes = []
    monkeypatch.setattr(cli, "save_config_value", lambda key, value: writes.append((key, value)))
    state = _stub()

    cli.HermesCLI._handle_reasoning_command(state, "/reasoning full")

    assert state.reasoning_full is True
    assert writes == [("display.reasoning_full", True)]


def test_reasoning_all_is_an_alias_for_full(monkeypatch):
    monkeypatch.setattr(cli, "save_config_value", lambda *_args: True)
    state = _stub()

    cli.HermesCLI._handle_reasoning_command(state, "/reasoning all")

    assert state.reasoning_full is True


def test_reasoning_clamp_restores_compact_recap(monkeypatch):
    writes = []
    monkeypatch.setattr(cli, "save_config_value", lambda key, value: writes.append((key, value)))
    state = _stub()
    state.reasoning_full = True

    cli.HermesCLI._handle_reasoning_command(state, "/reasoning clamp")

    assert state.reasoning_full is False
    assert writes == [("display.reasoning_full", False)]
